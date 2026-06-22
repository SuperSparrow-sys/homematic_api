import asyncio, json, ssl, uuid, os, threading, urllib.request, urllib.error, time
import websockets
from flask import Flask, jsonify, render_template, request
from pymodbus.server import StartTcpServer
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
from pymodbus.datastore.store import ModbusSparseDataBlock

HCU_IP = "192.168.0.90"
HCU_HOST = f"https://{HCU_IP}:6969"
HCU_WS  = f"wss://{HCU_IP}:9001"
PLUGIN_ID   = "de.local.hcu-bridge"
PLUGIN_NAME = {"de": "HCU Bridge"}
TOKEN_FILE  = os.path.join(os.path.dirname(__file__), "auth_token.json")
MODBUS_PORT = 502

app = Flask(__name__)
CACHE = {}
CACHE_LOCK = threading.Lock()
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

RAEUME = [
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
ROOM_COUNT = len(RAEUME)
HOLDING_GLOBAL = 0x1000
INPUT_GLOBAL   = 0x1000
ROOM_ID_BASE   = 0x2000

def _room_code(label):
    return label.split(" ")[0] if label else ""

ROOM_CODE_MAP = {_room_code(r): i for i, r in enumerate(RAEUME)}


class HoldingBlock(ModbusSparseDataBlock):
    def __init__(self):
        d = {}
        for i in range(ROOM_COUNT):
            a = i * 4
            d[a + 1] = 150; d[a + 2] = 1; d[a + 3] = 0; d[a + 4] = 0
        d[HOLDING_GLOBAL + 1] = ROOM_COUNT
        super().__init__(d)

    def setValues(self, address, values):
        super().setValues(address, values)
        for i, v in enumerate(values):
            _on_modbus_write(address + i, v)


class InputBlock(ModbusSparseDataBlock):
    def __init__(self):
        d = {}
        for i in range(ROOM_COUNT):
            a = i * 4
            d[a + 1] = 200; d[a + 2] = 0; d[a + 3] = 65535; d[a + 4] = 0
        d[INPUT_GLOBAL + 1] = 137; d[INPUT_GLOBAL + 2] = 720; d[INPUT_GLOBAL + 3] = 1
        super().__init__(d)


MB = None

def _gh(a): return MB[1].getValues(a + 1, 1)[0]
def _gi(a): return MB[2].getValues(a + 1, 1)[0]
def _sh(a, v): MB[1].setValues(a + 1, [v])
def _si(a, v): MB[2].setValues(a + 1, [v])


GID_BY_CODE = {}
_INTERNAL_SET = False

def _on_modbus_write(addr, val):
    global _INTERNAL_SET
    if _INTERNAL_SET:
        return
    room = (addr - 1) // 4
    offset = (addr - 1) % 4
    if room < 0 or room >= ROOM_COUNT:
        return
    code = _room_code(RAEUME[room])
    gid = GID_BY_CODE.get(code)
    if not gid:
        return
    try:
        if offset == 0:
            set_temp_sync(gid, val / 10)
        elif offset == 1:
            set_mode_sync(gid, ["AUTOMATIC", "ECO", "MANUAL"][val] if 0 <= val <= 2 else "ECO")
    except:
        pass


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
    except:
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
                CACHE["_last_update"] = __import__("datetime").datetime.now().isoformat()
            return True
    except: pass
    return False


def sync_modbus_loop():
    global _INTERNAL_SET
    while True:
        time.sleep(2)
        with CACHE_LOCK:
            groups = dict(CACHE.get("groups", {}))
            weather = CACHE.get("home", {}).get("weather", {})
        if not groups:
            continue
        for i in range(ROOM_COUNT):
            _si(ROOM_ID_BASE + i, i)
        if weather:
            _si(INPUT_GLOBAL, int(weather.get("temperature", 0) * 10))
            _si(INPUT_GLOBAL + 1, int(weather.get("humidity", 0)))
        for gid, grp in groups.items():
            if grp.get("type") != "HEATING":
                continue
            code = _room_code(grp.get("label", ""))
            i = ROOM_CODE_MAP.get(code)
            if i is None:
                continue
            GID_BY_CODE[code] = gid
            addr = i * 4
            ist  = int(grp.get("valveActualTemperature", 0) * 10)
            vent = int(grp.get("valvePosition", 0) * 1000)
            win  = {"OPEN": 1, "CLOSED": 0}.get(grp.get("windowState"), 65535)
            err  = (1 if grp.get("unreach") else 0) | (2 if grp.get("lowBat") else 0) | (4 if grp.get("heatingFailure") else 0)
            _si(addr, ist)
            _si(addr + 1, vent)
            _si(addr + 2, win)
            _si(addr + 3, err)
            # HR vom HCU-Cache spiegeln (SPS kann lesen)
            soll = int(grp.get("setPointTemperature", 15) * 10)
            mode = {"AUTOMATIC": 0, "ECO": 1, "MANUAL": 2}.get(grp.get("controlMode"), 1)
            boost = 1 if grp.get("boostMode") else 0
            party = 1 if grp.get("partyMode") else 0
            _INTERNAL_SET = True
            _sh(addr, soll)
            _sh(addr + 1, mode)
            _sh(addr + 2, boost)
            _sh(addr + 3, party)
            _INTERNAL_SET = False


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
    rooms = []
    for i, name in enumerate(RAEUME):
        addr = i * 4
        rooms.append({
            "i": i, "name": name,
            "room_id": _gi(ROOM_ID_BASE + i),
            "soll": _gh(addr),               "soll_c": _gh(addr) / 10,
            "mode_raw": _gh(addr + 1),
            "mode": ["AUTO","ECO","MANUAL"][_gh(addr + 1)] if _gh(addr + 1) <= 2 else "?",
            "boost": _gh(addr + 2),          "party": _gh(addr + 3),
            "ist": _gi(addr),                "ist_c": _gi(addr) / 10,
            "ventil": _gi(addr + 1),          "ventil_pct": _gi(addr + 1) / 10,
            "fenster": _gi(addr + 2),         "fehler": _gi(addr + 3),
        })
    return jsonify({
        "rooms": rooms,
        "aussentemp": _gi(INPUT_GLOBAL),
        "aussentemp_c": _gi(INPUT_GLOBAL) / 10,
        "feuchte": _gi(INPUT_GLOBAL + 1),
        "wetter": _gi(INPUT_GLOBAL + 2),
    })


@app.route("/api/refresh")
def api_refresh():
    return jsonify({"ok": update_cache()})


@app.route("/api/set-temp", methods=["POST"])
def api_set_temp():
    body = request.get_json(force=True)
    gid, temp = body.get("group_id"), body.get("temperature")
    if not gid or temp is None:
        return jsonify({"ok": False, "error": "group_id und temperature"}), 400
    ok, msg = set_temp_sync(gid, float(temp))
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/set-mode", methods=["POST"])
def api_set_mode():
    body = request.get_json(force=True)
    gid, mode = body.get("group_id"), body.get("mode")
    if not gid or not mode:
        return jsonify({"ok": False, "error": "group_id und mode"}), 400
    ok, msg = set_mode_sync(gid, mode)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/renew-token", methods=["POST"])
def api_renew_token():
    body = request.get_json(force=True)
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
    print("  Dashboard: http://localhost:5000")
    print("  Modbus:    localhost:%d" % MODBUS_PORT)
    app.run(host="0.0.0.0", port=5000, debug=False)
