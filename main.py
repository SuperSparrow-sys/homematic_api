import asyncio, json, logging, ssl, uuid, os, threading, urllib.request, urllib.error, time
from collections import deque
from datetime import datetime, timedelta
import websockets
from flask import Flask, jsonify, render_template, request
from pymodbus.server import StartTcpServer
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
from pymodbus.datastore.store import ModbusSparseDataBlock

from registers import (
    MODBUS_PORT, ROOM_STRIDE, MAX_ROOMS,
    OFF_SOLL, OFF_MODUS, OFF_BOOST, OFF_PARTY,
    OFF_IST, OFF_VENTIL, OFF_FENSTER, OFF_FEHLER,
    MODE_NAMES, HOLDING_GLOBAL, INPUT_GLOBAL, ROOM_ID_BASE,
    to_u16, from_i16, room_code as _room_code, room_id as _room_id,
)

HCU_IP = "172.168.1.124"
HCU_HOST = f"https://{HCU_IP}:6969"
HCU_WS  = f"wss://{HCU_IP}:9001"
PLUGIN_ID   = "de.local.hcu-bridge"
PLUGIN_NAME = {"de": "HCU Bridge"}
TOKEN_FILE  = os.path.join(os.path.dirname(__file__), "auth_token.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("hcu_bridge")

def log_exc(context):
    """Loggt die aktuelle Exception mit Kontext, statt sie stillschweigend zu
    verschlucken (siehe Analyse-Report Punkt 2.5). An allen Stellen verwenden,
    die bisher mit nacktem except: pass/except: Fehler ignoriert haben."""
    logger.exception("Fehler in %s", context)

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
CACHE = {}
CACHE_LOCK = threading.Lock()

# TLS: Standardmaessig wird die Verbindung zur HCU NICHT verifiziert, da die
# HCU ein selbstsigniertes Zertifikat verwendet (kein Hostname-Match moeglich).
# Um Man-in-the-Middle-Angriffe im lokalen Netz zu verhindern, kann das
# HCU-Zertifikat gepinnt werden: Zertifikat der HCU als PEM exportieren
# (z.B. via `openssl s_client -connect <HCU_IP>:6969 -showcerts`) und als
# hcu_ca.pem neben main.py ablegen. Ist die Datei vorhanden, wird ausschliesslich
# gegen dieses Zertifikat verifiziert; ohne Datei bleibt die bisherige,
# unverifizierte Verbindung als Fallback erhalten (mit Warnung beim Start).
HCU_CA_CERT = os.environ.get("HCU_CA_CERT", os.path.join(os.path.dirname(__file__), "hcu_ca.pem"))
ssl_ctx = ssl.create_default_context()
if os.path.exists(HCU_CA_CERT):
    ssl_ctx.load_verify_locations(HCU_CA_CERT)
    ssl_ctx.check_hostname = False  # HCU-Zertifikat ist i.d.R. nicht auf die IP ausgestellt
    ssl_ctx.verify_mode = ssl.CERT_REQUIRED
else:
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    logger.warning(
        "Kein gepinntes HCU-Zertifikat gefunden (%s) - TLS-Verifikation ist "
        "deaktiviert, Verbindung zur HCU ist gegen MITM im lokalen Netz nicht "
        "geschuetzt. Siehe README.md, Abschnitt TLS.", HCU_CA_CERT,
    )

ROOMS_FILE = os.path.join(os.path.dirname(__file__), "rooms.txt")
ROOMS_LOCK = threading.RLock()

_DEFAULT_ROOMS = [
    "A001 (Werkstatt)","A101 (Schleiferei)","A102 (QS)","A103 (Server)",
    "A201 (Umkleide Herren)","A202 (IT)","A203 (Vorraum)","A210 (Büro)",
    "A211 (Büro)","A213 (Besprechung)","C004 (TH)","C102 (Flur)",
    "C103 (AV)","C104 (Meister)","C106 (WC-D)","C107 (WC)",
    "C108 (WC-H)","C111 (Aufenthaltsraum)","C202 (Flur)","C203 (Büro)",
    "D003 (TH)","D004 (Umkleide)","D104 (Besprechung)","D105 (Einkauf)",
    "D203 (WC-D)","D204 (Konstruktion)","D302 (WC-H)","D303 (WC-D)",
    "D304 (Küche)","D305 (Projektleitung)","D306 (Abstellraum)","D307 (Besprechung)",
    "D308 (Besprechung)",
]

RAEUME = []
ROOM_COUNT = 0
ROOM_CODE_MAP = {}

def load_rooms():
    """Laedt rooms.txt in RAEUME/ROOM_COUNT/ROOM_CODE_MAP. Nur beim Start
    aufgerufen (vor Thread-Start), daher ohne ROOMS_LOCK unkritisch."""
    global RAEUME, ROOM_COUNT, ROOM_CODE_MAP
    if os.path.exists(ROOMS_FILE):
        with open(ROOMS_FILE, encoding="utf-8") as f:
            RAEUME = [line.strip() for line in f if line.strip()]
    else:
        RAEUME = list(_DEFAULT_ROOMS)
        with open(ROOMS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(RAEUME) + "\n")
    ROOM_COUNT = len(RAEUME)
    ROOM_CODE_MAP = {_room_code(r): i for i, r in enumerate(RAEUME)}
    if ROOM_COUNT > MAX_ROOMS:
        logger.warning(
            "rooms.txt enthaelt %d Raeume, aber nur %d Modbus-Slots sind "
            "vorallokiert (MAX_ROOMS in registers.py). Raeume ab Index %d "
            "sind fuer die SPS nicht erreichbar.", ROOM_COUNT, MAX_ROOMS, MAX_ROOMS,
        )

def persist_rooms():
    """Schreibt RAEUME vollstaendig nach rooms.txt. Einzige Schreibfunktion
    fuer diese Datei (ersetzt die vormals doppelte save_rooms()/rewrite_rooms()
    Logik aus Analyse-Report Punkt 2.7), damit rooms.txt nicht durch zwei
    unterschiedliche Schreibpfade auseinanderlaufen kann. Aufrufer muss
    ROOMS_LOCK bereits halten."""
    with open(ROOMS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(RAEUME) + "\n")

load_rooms()


class HoldingBlock(ModbusSparseDataBlock):
    def __init__(self):
        # Vorallokierung fuer MAX_ROOMS Slots statt nur ROOM_COUNT: pymodbus'
        # ModbusSparseDataBlock beantwortet nur Adressen, die hier beim Anlegen
        # registriert werden - kommt zur Laufzeit ein neuer Raum hinzu (>
        # ROOM_COUNT beim Start), waeren dessen Register sonst fuer die SPS
        # nicht erreichbar (siehe Analyse-Report Punkt 2.3).
        d = {}
        for i in range(MAX_ROOMS):
            a = i * ROOM_STRIDE
            d[a + 1] = 0; d[a + 2] = 0; d[a + 3] = 0; d[a + 4] = 0
        d[HOLDING_GLOBAL + 1] = ROOM_COUNT
        super().__init__(d)

    def setValues(self, address, values):
        super().setValues(address, values)
        for i, v in enumerate(values):
            _on_modbus_write(address + i, v)


class InputBlock(ModbusSparseDataBlock):
    def __init__(self):
        d = {}
        for i in range(MAX_ROOMS):
            a = i * ROOM_STRIDE
            d[a + 1] = 0; d[a + 2] = 0; d[a + 3] = 65535; d[a + 4] = 0
        d[INPUT_GLOBAL + 1] = 0; d[INPUT_GLOBAL + 2] = 0; d[INPUT_GLOBAL + 3] = 0
        for i in range(MAX_ROOMS):
            d[ROOM_ID_BASE + i + 1] = 65535
        super().__init__(d)


MB = None

# Die Modbus-Register hier sind 1-basiert adressiert (pymodbus-Konvention der
# genutzten Version), waehrend Raum-/Offset-Rechnungen im restlichen Code
# 0-basiert sind - daher das +1 in jedem Zugriff.
def read_holding(addr): return MB[1].getValues(addr + 1, 1)[0]
def read_input(addr):   return MB[2].getValues(addr + 1, 1)[0]
def write_holding(addr, val): MB[1].setValues(addr + 1, [val])
def write_input(addr, val):   MB[2].setValues(addr + 1, [val])


GID_BY_CODE = {}
_INTERNAL_SET = False
# Schuetzt _INTERNAL_SET gegen den SPS-Schreibthread (siehe Analyse-Report
# Punkt G3): ohne Lock konnte ein SPS-Schreibzugriff, der exakt in das
# kurze Fenster zwischen _INTERNAL_SET=True und =False fiel, faelschlich als
# interner Echo erkannt und stillschweigend verworfen werden. RLock statt
# Lock, weil der Sync-Thread ueber write_holding() synchron in denselben
# HoldingBlock.setValues()-Callback zurueckspringt, der den Lock haelt -
# ein einfaches Lock wuerde sich hier selbst blockieren (Deadlock).
_INTERNAL_LOCK = threading.RLock()

CMD_QUEUE = deque()
CMD_LOCK = threading.Lock()

def _enqueue_cmd(cmd_type, gid, val):
    with CMD_LOCK:
        CMD_QUEUE.append((cmd_type, gid, val))

def _process_queue():
    # Intervall/Batching hier bewusst unveraendert gelassen (siehe Report 2.1) -
    # nur Fehlerbehandlung ergaenzt, damit fehlgeschlagene HCU-Befehle nicht
    # mehr stillschweigend verschwinden.
    while True:
        time.sleep(300)
        item = None
        with CMD_LOCK:
            if CMD_QUEUE:
                item = CMD_QUEUE.popleft()
        if item:
            typ, gid, val = item
            try:
                if typ == "temp":
                    set_temp_sync(gid, val)
                elif typ == "mode":
                    set_mode_sync(gid, val)
                elif typ == "boost":
                    set_boost_sync(gid, val)
                elif typ == "party":
                    set_party_sync(gid, val)
            except Exception:
                log_exc(f"_process_queue({typ}, {gid})")

def _on_modbus_write(addr, val):
    global _INTERNAL_SET
    with _INTERNAL_LOCK:
        if _INTERNAL_SET:
            return
        room = (addr - 1) // ROOM_STRIDE
        offset = (addr - 1) % ROOM_STRIDE
        with ROOMS_LOCK:
            if room < 0 or room >= ROOM_COUNT:
                return
            code = _room_code(RAEUME[room])
            gid = GID_BY_CODE.get(code)
        if not gid:
            return
        if offset == OFF_SOLL:
            # Dieselbe Grenzpruefung wie /api/set-temp (siehe Analyse-Report
            # Punkt M1): ohne sie wuerde ein fehlerhafter SPS-Rohwert
            # ungeprueft als Sollwert an die HCU weitergereicht.
            temp = from_i16(val) / 10
            if not (MIN_TEMP <= temp <= MAX_TEMP):
                logger.warning(
                    "SPS schreibt ungueltige Solltemp %.1f fuer Raum %s "
                    "(Rohwert %d) - verworfen, gueltig ist %.1f-%.1f.",
                    temp, code, val, MIN_TEMP, MAX_TEMP,
                )
                return
            _enqueue_cmd("temp", gid, temp)
        elif offset == OFF_MODUS:
            # Unbekannte Moduscodes werden verworfen statt sie still auf ECO
            # abzubilden (siehe Analyse-Report Punkt M1).
            if not (0 <= val <= 2):
                logger.warning(
                    "SPS schreibt unbekannten Modus-Code %d fuer Raum %s - verworfen.",
                    val, code,
                )
                return
            _enqueue_cmd("mode", gid, MODE_NAMES[val])
        elif offset == OFF_BOOST:
            _enqueue_cmd("boost", gid, bool(val))
        elif offset == OFF_PARTY:
            _enqueue_cmd("party", gid, bool(val))


def rest_post(path, body):
    url = f"{HCU_HOST}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json", "VERSION": "12"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise Exception(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")


def load_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            d = json.load(f)
        return d.get("auth_token"), d.get("client_id")
    return None, None


def save_token(auth_token, client_id):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"auth_token": auth_token, "client_id": client_id}, f)


def renew_token(activation_key):
    try:
        resp1 = rest_post("/hmip/auth/requestConnectApiAuthToken", {
            "activationKey": activation_key, "pluginId": PLUGIN_ID, "friendlyName": PLUGIN_NAME,
        })
        auth_token = resp1.get("authToken")
        if not auth_token:
            return False, "Kein authToken: " + json.dumps(resp1)[:200]
        resp2 = rest_post("/hmip/auth/confirmConnectApiAuthToken", {
            "activationKey": activation_key, "authToken": auth_token,
        })
        client_id = resp2.get("clientId")
        if not client_id:
            return False, "Kein clientId: " + json.dumps(resp2)[:200]
        save_token(auth_token, client_id)
        return True, "Authentifizierung erfolgreich"
    except Exception as e:
        return False, f"Fehler: {e}"


async def _send_ws_command(auth_token, path, body, timeout=8):
    headers = {"authtoken": auth_token, "plugin-id": PLUGIN_ID, "hmip-system-events": "true"}
    async with websockets.connect(HCU_WS, ssl=ssl_ctx, additional_headers=headers, max_size=2**23) as ws:
        await ws.send(json.dumps({"id": str(uuid.uuid4()), "pluginId": PLUGIN_ID,
            "type": "PLUGIN_STATE_RESPONSE",
            "body": {"pluginReadinessStatus": "READY", "friendlyName": PLUGIN_NAME}}))
        await asyncio.sleep(0.3)
        req_id = str(uuid.uuid4())
        await ws.send(json.dumps({"id": req_id, "pluginId": PLUGIN_ID,
            "type": "HMIP_SYSTEM_REQUEST", "body": {"path": path, "body": body}}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "HMIP_SYSTEM_RESPONSE" and msg.get("id") == req_id:
                code = msg.get("body", {}).get("code")
                if code == 200: return True, "ok"
                return False, f"Fehler {code}: {msg.get('body',{}).get('body',{})}"
            if t in ("DISCOVER_REQUEST", "PLUGIN_STATE_REQUEST", "HMIP_SYSTEM_EVENT"):
                continue
            return False, f"Unerwartet: {t}"


def set_temp_sync(group_id, temperature):
    auth_token, _ = load_token()
    if not auth_token: return False, "Kein Auth-Token"
    return asyncio.run(_send_ws_command(auth_token, "/hmip/group/heating/setSetPointTemperature",
        {"groupId": group_id, "setPointTemperature": round(temperature, 1)}))


def set_mode_sync(group_id, mode):
    auth_token, _ = load_token()
    if not auth_token: return False, "Kein Auth-Token"
    return asyncio.run(_send_ws_command(auth_token, "/hmip/group/heating/setControlMode",
        {"groupId": group_id, "controlMode": mode}))


# Boost/Party werden laut REGISTERMAP.md ueber HR+2/HR+3 von der SPS
# schreibbar dokumentiert, wurden bisher aber nirgends an die HCU
# weitergegeben (siehe Analyse-Report Punkt 2.6). HINWEIS: Die HCU-Endpunkte
# hier folgen dem allgemein bekannten HomematicIP-Cloud-API-Muster (analog zu
# setSetPointTemperature/setControlMode oben), sind aber - anders als die
# bereits produktiv genutzten Aufrufe - NICHT gegen eine echte HCU getestet.
# Vor produktivem Einsatz unbedingt einmal kontrolliert testen (z.B. Boost per
# Modbus-Testschreibzugriff ausloesen und in der Homematic-App gegenpruefen).
PARTY_MODE_DURATION_MINUTES = 120  # SPS liefert nur 0/1 ueber Modbus, keine Zeitangabe -> feste Default-Dauer

def _mode_before_override(group_id):
    """Liest den zuletzt bekannten Regelmodus einer Heizgruppe aus dem
    HCU-Cache, um ihn nach Boost/Party wiederherzustellen (siehe Analyse-
    Report Punkt M2), statt beim Beenden hart auf AUTOMATIC umzuschalten und
    damit einen zuvor bewusst gesetzten MANUAL/ECO-Modus zu ueberschreiben.
    Faellt auf AUTOMATIC zurueck, wenn der Cache keinen bekannten Modus liefert."""
    with CACHE_LOCK:
        grp = CACHE.get("groups", {}).get(group_id) or {}
    mode = grp.get("controlMode")
    return mode if mode in MODE_NAMES else "AUTOMATIC"


def set_boost_sync(group_id, activate):
    auth_token, _ = load_token()
    if not auth_token: return False, "Kein Auth-Token"
    if activate:
        return asyncio.run(_send_ws_command(auth_token, "/hmip/group/heating/setBoost",
            {"groupId": group_id}))
    # Boost-Deaktivierung ueber die HCU-API erfolgt durch Zurueckschalten in
    # den zuvor aktiven Modus (nicht mehr hart AUTOMATIC, siehe M2 oben).
    return set_mode_sync(group_id, _mode_before_override(group_id))


def set_party_sync(group_id, activate):
    auth_token, _ = load_token()
    if not auth_token: return False, "Kein Auth-Token"
    if activate:
        start = datetime.now()
        end = start + timedelta(minutes=PARTY_MODE_DURATION_MINUTES)
        body = {
            "groupId": group_id,
            "temperature": 21.0,
            "startTime": start.strftime("%Y_%m_%d %H:%M"),
            "endTime": end.strftime("%Y_%m_%d %H:%M"),
        }
        return asyncio.run(_send_ws_command(auth_token, "/hmip/group/heating/setPartyMode", body))
    return set_mode_sync(group_id, _mode_before_override(group_id))


async def fetch_system_state(auth_token):
    headers = {"authtoken": auth_token, "plugin-id": PLUGIN_ID, "hmip-system-events": "false"}
    try:
        async with websockets.connect(HCU_WS, ssl=ssl_ctx, additional_headers=headers, max_size=2**23) as ws:
            await ws.send(json.dumps({"id": str(uuid.uuid4()), "pluginId": PLUGIN_ID,
                "type": "PLUGIN_STATE_RESPONSE",
                "body": {"pluginReadinessStatus": "READY", "friendlyName": PLUGIN_NAME}}))
            await asyncio.sleep(0.3)
            rid = str(uuid.uuid4())
            await ws.send(json.dumps({"id": rid, "pluginId": PLUGIN_ID,
                "type": "HMIP_SYSTEM_REQUEST",
                "body": {"path": "/hmip/home/getSystemState", "body": {}}}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                if msg.get("type") == "HMIP_SYSTEM_RESPONSE" and msg.get("id") == rid:
                    b = msg.get("body", {})
                    return b.get("body", {}) if b.get("code") == 200 else {}
    except Exception:
        log_exc("fetch_system_state")
        return {}


def update_cache():
    global CACHE
    auth_token, _ = load_token()
    if not auth_token: return False
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        data = loop.run_until_complete(fetch_system_state(auth_token))
        loop.close()
        if data:
            with CACHE_LOCK:
                CACHE.update(data)
                CACHE["_last_update"] = datetime.now().isoformat()
            return True
    except Exception:
        log_exc("update_cache")
    return False


def sync_modbus_once(groups, weather):
    global _INTERNAL_SET, ROOM_COUNT, ROOM_CODE_MAP
    if not groups:
        return
    with ROOMS_LOCK:
        for i in range(ROOM_COUNT):
            # room_id() statt des Index i selbst (siehe Analyse-Report Punkt
            # H1): eine Pruefsumme aus dem Raumcode erkennt eine tatsaechliche
            # Verschiebung der Raumreihenfolge, der Index selbst kann das
            # strukturell nie tun, da er hier immer sich selbst zurueckschreibt.
            write_input(ROOM_ID_BASE + i, _room_id(RAEUME[i]))
        write_holding(HOLDING_GLOBAL, ROOM_COUNT)
    if weather:
        # to_u16(): Aussentemperatur kann negativ sein (siehe Analyse-Report
        # Punkt K1) - ohne Zweierkomplement-Kodierung kann die SPS das
        # Register dann nicht mehr lesen. round() statt int() vermeidet
        # zudem den Abschneide-Bias bei negativen Werten (Punkt G2).
        write_input(INPUT_GLOBAL, to_u16(round(weather.get("temperature", 0) * 10)))
        write_input(INPUT_GLOBAL + 1, round(weather.get("humidity", 0)))
    for gid, grp in groups.items():
        if grp.get("type") != "HEATING":
            continue
        code = _room_code((grp.get("label") or "").strip())
        if not code:
            continue
        with ROOMS_LOCK:
            i = ROOM_CODE_MAP.get(code)
            if i is None:
                if ROOM_COUNT >= MAX_ROOMS:
                    logger.warning(
                        "Neuer Raum '%s' erkannt, aber MAX_ROOMS=%d bereits "
                        "ausgeschoepft - Raum wird ignoriert. MAX_ROOMS in "
                        "registers.py erhoehen.", code, MAX_ROOMS,
                    )
                    continue
                label = (grp.get("label") or "").strip()
                i = ROOM_COUNT
                RAEUME.append(label)
                ROOM_CODE_MAP[code] = i
                ROOM_COUNT = i + 1
                persist_rooms()
            GID_BY_CODE[code] = gid
        addr = i * ROOM_STRIDE
        # round() statt int() (Punkt G2), to_u16() fuer die Isttemperatur, da
        # sie in unbeheizten Raeumen unter 0 Grad fallen kann (Punkt K1).
        ist  = round((grp.get("valveActualTemperature") or 0) * 10)
        vent = round((grp.get("valvePosition") or 0) * 1000)
        win  = {"OPEN": 1, "CLOSED": 0}.get(grp.get("windowState"), 65535)
        err  = (1 if grp.get("unreach") else 0) | (2 if grp.get("lowBat") else 0) | (4 if grp.get("heatingFailure") else 0)
        write_input(addr + OFF_IST, to_u16(ist))
        write_input(addr + OFF_VENTIL, vent)
        write_input(addr + OFF_FENSTER, win)
        write_input(addr + OFF_FEHLER, err)
        soll = round((grp.get("setPointTemperature") or 15) * 10)
        mode = {"AUTOMATIC": 0, "ECO": 1, "MANUAL": 2}.get(grp.get("controlMode"), 1)
        boost = 1 if grp.get("boostMode") else 0
        party = 1 if grp.get("partyMode") else 0
        with _INTERNAL_LOCK:
            _INTERNAL_SET = True
            write_holding(addr + OFF_SOLL, to_u16(soll))
            write_holding(addr + OFF_MODUS, mode)
            write_holding(addr + OFF_BOOST, boost)
            write_holding(addr + OFF_PARTY, party)
            _INTERNAL_SET = False

def sync_modbus_loop():
    global _INTERNAL_SET, ROOM_COUNT, ROOM_CODE_MAP
    tick = 0
    while True:
        time.sleep(2)
        tick += 1
        if tick % 15 == 0:
            update_cache()
        with CACHE_LOCK:
            groups = dict(CACHE.get("groups", {}))
            weather = CACHE.get("home", {}).get("weather", {})
        sync_modbus_once(groups, weather)


MIN_TEMP, MAX_TEMP = 5.0, 30.0

def _parse_json_body():
    """Parst den Request-Body als JSON und liefert (body, error_response).
    Faengt kaputtes JSON ab statt es als unbehandelte Exception (HTTP 500)
    durchschlagen zu lassen (siehe Analyse-Report Punkt 1.6)."""
    try:
        body = request.get_json(force=True, silent=False)
        if not isinstance(body, dict):
            return None, (jsonify({"ok": False, "error": "JSON-Objekt erwartet"}), 400)
        return body, None
    except Exception:
        return None, (jsonify({"ok": False, "error": "Ungueltiges JSON"}), 400)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    with CACHE_LOCK:
        if not CACHE:
            return jsonify({"error": "Keine Daten"}), 503
        d = dict(CACHE)
    groups  = d.get("groups", {})
    home    = d.get("home", {})
    heating = []
    for gid, grp in groups.items():
        if grp.get("type") == "HEATING":
            heating.append({
                "id": gid, "raum": grp.get("label", "?"),
                "ist_temp": grp.get("valveActualTemperature"),
                "soll_temp": grp.get("setPointTemperature"),
                "ventil": grp.get("valvePosition"),
                "modus": grp.get("controlMode"),
                "boost": grp.get("boostMode"),
                "fenster": grp.get("windowState"),
                "heating_failure": grp.get("heatingFailure"),
                "unreach": grp.get("unreach"),
                "lowbat": grp.get("lowBat"),
                "party": grp.get("partyMode"),
            })
    weather = home.get("weather", {})
    return jsonify({
        "timestamp": d.get("_last_update", ""),
        "weather": weather,
        "heating": sorted(heating, key=lambda x: x["raum"] or ""),
        "heating_count": len(heating),
    })


@app.route("/api/modbus")
def api_modbus():
    with ROOMS_LOCK:
        raeume_snapshot = list(RAEUME)
    rooms = []
    for i, name in enumerate(raeume_snapshot):
        addr = i * ROOM_STRIDE
        mode_raw = read_holding(addr + OFF_MODUS)
        # from_i16(): Soll-/Isttemperatur sind vorzeichenbehaftet im Register
        # kodiert (siehe Analyse-Report Punkt K1) und muessen beim Anzeigen
        # entsprechend zurueckgewandelt werden.
        soll = from_i16(read_holding(addr + OFF_SOLL))
        ist  = from_i16(read_input(addr + OFF_IST))
        rooms.append({
            "i": i, "name": name,
            "room_id": read_input(ROOM_ID_BASE + i),
            "soll": soll, "soll_c": soll / 10,
            "mode_raw": mode_raw,
            "mode": MODE_NAMES[mode_raw] if mode_raw <= 2 else "?",
            "boost": read_holding(addr + OFF_BOOST), "party": read_holding(addr + OFF_PARTY),
            "ist": ist, "ist_c": ist / 10,
            "ventil": read_input(addr + OFF_VENTIL), "ventil_pct": read_input(addr + OFF_VENTIL) / 10,
            "fenster": read_input(addr + OFF_FENSTER), "fehler": read_input(addr + OFF_FEHLER),
        })
    aussentemp = from_i16(read_input(INPUT_GLOBAL))
    return jsonify({
        "rooms": rooms,
        "aussentemp": aussentemp,
        "aussentemp_c": aussentemp / 10,
        "feuchte": read_input(INPUT_GLOBAL + 1),
        "wetter": read_input(INPUT_GLOBAL + 2),
    })


@app.route("/api/move-room/<int:idx>/<direction>", methods=["POST"])
def api_move_room(idx, direction):
    global ROOM_CODE_MAP
    with ROOMS_LOCK:
        if direction == "up" and idx > 0:
            RAEUME[idx], RAEUME[idx-1] = RAEUME[idx-1], RAEUME[idx]
        elif direction == "down" and idx < ROOM_COUNT - 1:
            RAEUME[idx], RAEUME[idx+1] = RAEUME[idx+1], RAEUME[idx]
        else:
            return jsonify({"ok": False}), 400
        ROOM_CODE_MAP = {_room_code(r): i for i, r in enumerate(RAEUME)}
        write_holding(HOLDING_GLOBAL, ROOM_COUNT)
        persist_rooms()
    return jsonify({"ok": True})

@app.route("/api/registers")
def api_registers():
    with ROOMS_LOCK:
        room_count = ROOM_COUNT
    regs = []
    for i in range(room_count):
        a = i * ROOM_STRIDE
        for off in range(4):
            regs.append({"a": hex(a + off), "hr": read_holding(a + off), "ir": read_input(a + off)})
    for off in range(3):
        # HOLDING_GLOBAL == INPUT_GLOBAL (beide 0x1000), aber HR und IR sind
        # getrennte Adressraeume: an Offset 0 liegt sowohl die Raumanzahl (HR,
        # read-only fuer die SPS) als auch die Aussentemperatur (IR). Bisher
        # wurde hier fuer alle drei Zeilen hart "-" statt des echten HR-Werts
        # ausgegeben, wodurch die Raumanzahl im Register-Dump nie sichtbar war
        # (siehe Analyse-Report Punkt H2).
        hr_val = read_holding(HOLDING_GLOBAL) if off == 0 else "-"
        regs.append({"a": hex(INPUT_GLOBAL + off), "hr": hr_val, "ir": read_input(INPUT_GLOBAL + off)})
    for i in range(room_count):
        regs.append({"a": hex(ROOM_ID_BASE + i), "hr": "-", "ir": read_input(ROOM_ID_BASE + i)})
    return jsonify({"regs": regs})

@app.route("/api/raw")
def api_raw():
    with CACHE_LOCK:
        if not CACHE:
            return jsonify({"error": "Keine Daten"}), 503
        return jsonify(dict(CACHE))

@app.route("/api/refresh")
def api_refresh():
    ok = update_cache()
    if ok:
        with CACHE_LOCK:
            groups = dict(CACHE.get("groups", {}))
            weather = CACHE.get("home", {}).get("weather", {})
        sync_modbus_once(groups, weather)
    return jsonify({"ok": ok})


@app.route("/api/set-temp", methods=["POST"])
def api_set_temp():
    body, err = _parse_json_body()
    if err: return err
    gid, temp = body.get("group_id"), body.get("temperature")
    if not gid or temp is None:
        return jsonify({"ok": False, "error": "group_id und temperature"}), 400
    try:
        temp = float(temp)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "temperature muss eine Zahl sein"}), 400
    if not (MIN_TEMP <= temp <= MAX_TEMP):
        return jsonify({"ok": False, "error": f"temperature muss zwischen {MIN_TEMP} und {MAX_TEMP} liegen"}), 400
    _enqueue_cmd("temp", gid, temp)
    return jsonify({"ok": True, "msg": "eingereiht"})


@app.route("/api/set-mode", methods=["POST"])
def api_set_mode():
    body, err = _parse_json_body()
    if err: return err
    gid, mode = body.get("group_id"), body.get("mode")
    if not gid or not mode:
        return jsonify({"ok": False, "error": "group_id und mode"}), 400
    if mode not in MODE_NAMES:
        return jsonify({"ok": False, "error": f"mode muss einer von {MODE_NAMES} sein"}), 400
    _enqueue_cmd("mode", gid, mode)
    return jsonify({"ok": True, "msg": "eingereiht"})


@app.route("/api/renew-token", methods=["POST"])
def api_renew_token():
    body, err = _parse_json_body()
    if err: return err
    key = body.get("activation_key")
    if not key: return jsonify({"ok": False, "error": "activation_key"}), 400
    ok, msg = renew_token(str(key).strip())
    return jsonify({"ok": ok, "msg": msg})


if __name__ == "__main__":
    print("  Starte Modbus-Server auf Port %d ..." % MODBUS_PORT)
    hb = HoldingBlock()
    ib = InputBlock()
    store = ModbusSlaveContext(di=ModbusSparseDataBlock({}), co=ModbusSparseDataBlock({}), hr=hb, ir=ib)
    ctx = ModbusServerContext(slaves=store, single=True)
    MB = (ctx, hb, ib)

    t = threading.Thread(target=lambda: StartTcpServer(context=ctx, address=("0.0.0.0", MODBUS_PORT)), daemon=True)
    t.start()

    if update_cache():
        print("  Cache geladen: %d Gruppen" % len(CACHE.get("groups", {})))
    else:
        print("  [WARN] Kein Auth-Token oder HCU nicht erreichbar.")
        print("  Bitte http://localhost:5000 aufrufen und Auth-Schluessel eingeben.")

    threading.Thread(target=sync_modbus_loop, daemon=True).start()
    threading.Thread(target=_process_queue, daemon=True).start()
    print("  Dashboard: http://localhost:5000")
    print("  Modbus:    localhost:%d" % MODBUS_PORT)
    app.run(host="0.0.0.0", port=5000, debug=False)
