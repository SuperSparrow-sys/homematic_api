import asyncio, json, ssl, uuid, os, threading, urllib.request, urllib.error, random, math, time
import websockets
from flask import Flask, jsonify, render_template_string, request
from pymodbus.server import StartTcpServer
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
from pymodbus.datastore.store import ModbusSparseDataBlock

# ─── Konfiguration ─────────────────────────────────────────────────
HCU_IP = "192.168.0.90"
HCU_HOST = f"https://{HCU_IP}:6969"
HCU_WS  = f"wss://{HCU_IP}:9001"
PLUGIN_ID   = "de.local.hcu-bridge"
PLUGIN_NAME = {"de": "HCU Bridge"}
TOKEN_FILE  = os.path.join(os.path.dirname(__file__), "auth_token.json")

SIMULATE = True        # True = Fake-Daten, False = echte HCU
MODBUS_PORT = 5020     # Port fuer SPS

app = Flask(__name__)
CACHE = {}
CACHE_LOCK = threading.Lock()
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

RAEUME = [
    "A001 Werkstatt","A101 Schleiferei","A102 QS","A103 Server",
    "A201 Umkleide H","A202 IT","A203 Vorraum","A210 Buro",
    "A211 Buro","A213 Besprechung","C004 TH","C102 Flur",
    "C103 AV","C104 Meister","C106 WC-D","C107 WC",
    "C108 WC-H","C111 Aufenthalt","C202 Flur","C203 Buro",
    "D003 TH","D004 Umkleide","D104 Besprechung","D105 Einkauf",
    "D203 WC-D","D204 Konstruktion","D302 WC-H","D303 WC-D",
    "D304 Kuche","D305 ProjektL","D306 Abstell","D307 Besprechung",
    "D308 Besprechung",
]
ROOM_COUNT = len(RAEUME)
HOLDING_GLOBAL = 0x1000
INPUT_GLOBAL   = 0x1000

# ─── Modbus Datenblöcke ────────────────────────────────────────────

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

MB = None  # (context, hr_store, ir_store)

def _gh(a): return MB[1].getValues(a + 1, 1)[0]
def _gi(a): return MB[2].getValues(a + 1, 1)[0]
def _sh(a, v): MB[1].setValues(a + 1, [v])
def _si(a, v): MB[2].setValues(a + 1, [v])

def _on_modbus_write(addr, val):
    """Wird aufgerufen wenn die SPS ein Holding-Register schreibt."""
    room = (addr - 1) // 4
    offset = (addr - 1) % 4
    if room < 0 or room >= ROOM_COUNT:
        return
    if SIMULATE:
        with CACHE_LOCK:
            gid = f"SIM_GROUP_{room:04d}"
            grp = CACHE.get("groups", {}).get(gid)
            if not grp:
                return
            if offset == 0:
                grp["setPointTemperature"] = val / 10
            elif offset == 1:
                grp["controlMode"] = ["AUTO", "ECO", "MANUAL"][val] if 0 <= val <= 2 else grp["controlMode"]
            elif offset == 2:
                grp["boostMode"] = bool(val)
        return
    # Echte HCU: per WS schreiben
    try:
        gid = list(CACHE.get("groups", {}).keys())[room]
        if offset == 0:
            set_temp_sync(gid, val / 10)
        elif offset == 1:
            set_mode_sync(gid, ["AUTO", "ECO", "MANUAL"][val] if 0 <= val <= 2 else "ECO")
    except:
        pass


# ─── HCU Auth ──────────────────────────────────────────────────────

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
            "activationKey": activation_key,
            "pluginId": PLUGIN_ID,
            "friendlyName": PLUGIN_NAME,
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


# ─── Simulierte HCU ────────────────────────────────────────────────

def _simulate_data():
    now = __import__("datetime").datetime.now()
    t = now.timestamp() / 60
    groups, devices = {}, {}
    for i, name in enumerate(RAEUME):
        gid = f"SIM_GROUP_{i:04d}"; did = f"SIM_DEV_{i:04d}"
        soll = 15.0 + 5 * (math.sin(i * 1.7 + t * 0.02) * 0.5 + 0.5)
        ist  = soll + (math.sin(i + t * 0.1) * 0.8 - 0.3)
        mode_idx = int(math.sin(i * 2.3 + t * 0.05) * 1.5 + 1.5)
        mode = ["AUTO", "ECO", "MANUAL"][mode_idx]
        vent = max(0, min(1, (soll - ist) * 0.1 + 0.1 + random.gauss(0, 0.05)))
        win = (math.sin(i * 3.1 + t * 0.015) > 0.92)
        unreach = (math.sin(i * 5.7 + t * 0.003) > 0.98)
        lowbat  = (math.sin(i * 7.1 + t * 0.007) > 0.97)
        hfail   = (math.sin(i * 11.3 + t * 0.011) > 0.99)
        boost   = (math.sin(i * 13.7 + t * 0.023) > 0.95)
        groups[gid] = {"id": gid, "type": "HEATING", "label": name,
            "valveActualTemperature": round(ist, 1), "setPointTemperature": round(soll, 1),
            "valvePosition": round(vent, 3), "controlMode": mode, "boostMode": boost,
            "windowState": "OPEN" if win else "CLOSED", "partyMode": False, "cooling": False,
            "minTemperature": 5.0, "maxTemperature": 30.0,
            "heatingFailure": hfail, "unreach": unreach, "lowBat": lowbat,
            "dutyCycle": round(random.uniform(0, 0.3), 3), "activeProfile": "Wohnen",
            "floorHeatingMode": False, "humidityLimitValue": 70}
        devices[did] = {"id": did, "type": "HEATING_THERMOSTAT", "label": name,
            "modelType": "HMIP-eTRV-2", "firmwareVersion": "1.12.6",
            "permanentlyReachable": not unreach,
            "functionalChannels": {"1": {"functionalChannelType": "HEATING_CLIMATE_CONTROL",
                "label": name, "groups": [gid], "unreach": unreach, "lowBat": lowbat,
                "windowState": "OPEN" if win else "CLOSED",
                "valveActualTemperature": round(ist, 1), "setPointTemperature": round(soll, 1),
                "valvePosition": round(vent, 3), "controlMode": mode}}}
    home = {"id": "SIM_HOME_001", "currentAPVersion": "3.8.6",
        "location": {"city": "Dresden", "country": "DE"},
        "functionalHomes": {"INDOOR_CLIMATE": {"id": "SIM_IC_001"}},
        "weather": {"temperature": round(12.0 + math.sin(t * 0.01) * 5, 1),
            "humidity": round(65 + math.sin(t * 0.005) * 15, 0),
            "weatherCondition": "WOLKY", "weatherConditionCode": 2}}
    return {"groups": groups, "devices": devices, "home": home}


# ─── Cache / Modbus Sync ───────────────────────────────────────────

def update_cache():
    global CACHE
    if SIMULATE:
        with CACHE_LOCK:
            CACHE = _simulate_data()
            CACHE["_last_update"] = __import__("datetime").datetime.now().isoformat()
        return True
    # --- echte HCU ---
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
    while True:
        time.sleep(2)
        with CACHE_LOCK:
            groups = dict(CACHE.get("groups", {}))
            weather = CACHE.get("home", {}).get("weather", {})
        if not groups:
            continue
        # Globale IR updaten
        if weather:
            _si(INPUT_GLOBAL, int(weather.get("temperature", 0) * 10))
            _si(INPUT_GLOBAL + 1, int(weather.get("humidity", 0)))
        # Raeume
        for i, (gid, grp) in enumerate(sorted(groups.items())):
            if grp.get("type") != "HEATING":
                continue
            if i >= ROOM_COUNT:
                break
            addr = i * 4
            ist  = int(grp.get("valveActualTemperature", 0) * 10)
            vent = int(grp.get("valvePosition", 0) * 1000)
            win  = {"OPEN": 1, "CLOSED": 0}.get(grp.get("windowState"), 65535)
            err  = (1 if grp.get("unreach") else 0) | (2 if grp.get("lowBat") else 0) | (4 if grp.get("heatingFailure") else 0)
            _si(addr, ist)
            _si(addr + 1, vent)
            _si(addr + 2, win)
            _si(addr + 3, err)
            # HR nur im Sim-Modus setzen (bei echter HCU schreibt nur die SPS)
            if SIMULATE:
                soll = int(grp.get("setPointTemperature", 15) * 10)
                mode = {"AUTO": 0, "ECO": 1, "MANUAL": 2}.get(grp.get("controlMode"), 1)
                _sh(addr, soll)
                _sh(addr + 1, mode)
                _sh(addr + 2, 1 if grp.get("boostMode") else 0)


# ─── Dashboard (HCU + Modbus) ──────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

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
        "simulate": SIMULATE,
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
    if SIMULATE:
        with CACHE_LOCK:
            grp = CACHE.get("groups", {}).get(gid)
            if grp: grp["setPointTemperature"] = float(temp)
        return jsonify({"ok": True, "msg": "ok (simuliert)"})
    ok, msg = set_temp_sync(gid, float(temp))
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/set-mode", methods=["POST"])
def api_set_mode():
    body = request.get_json(force=True)
    gid, mode = body.get("group_id"), body.get("mode")
    if not gid or not mode:
        return jsonify({"ok": False, "error": "group_id und mode"}), 400
    if SIMULATE:
        with CACHE_LOCK:
            grp = CACHE.get("groups", {}).get(gid)
            if grp: grp["controlMode"] = mode
        return jsonify({"ok": True, "msg": "ok (simuliert)"})
    ok, msg = set_mode_sync(gid, mode)
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/renew-token", methods=["POST"])
def api_renew_token():
    body = request.get_json(force=True)
    key = body.get("activation_key")
    if not key: return jsonify({"ok": False, "error": "activation_key"}), 400
    ok, msg = renew_token(str(key).strip())
    return jsonify({"ok": ok, "msg": msg})


# ─── Start ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("  Starte Modbus-Server auf Port %d ..." % MODBUS_PORT)
    hb = HoldingBlock()
    ib = InputBlock()
    store = ModbusSlaveContext(di=ModbusSparseDataBlock({}), co=ModbusSparseDataBlock({}), hr=hb, ir=ib)
    ctx = ModbusServerContext(slaves=store, single=True)
    MB = (ctx, hb, ib)

    t = threading.Thread(target=lambda: StartTcpServer(context=ctx, address=("0.0.0.0", MODBUS_PORT)), daemon=True)
    t.start()

    if SIMULATE:
        print("  [SIMULATION] Fake-Daten aktiv (SIMULATE = True)")
        update_cache()
    elif update_cache():
        print("  Cache geladen")
    else:
        print("  [WARN] Konnte keine HCU-Daten laden. SIMULATE = True setzen fuer Test.")

    threading.Thread(target=sync_modbus_loop, daemon=True).start()
    print("  Dashboard: http://localhost:5000")
    print("  Modbus:    localhost:%d" % MODBUS_PORT)
    app.run(host="0.0.0.0", port=5000, debug=False)


HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HCU Bridge</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,sans-serif;background:#f5f5f5;color:#111;padding:16px}
h1{font-size:1.2rem;margin-bottom:4px}
.tabs{display:flex;gap:8px;margin-bottom:12px}
.tabs button{padding:6px 16px;border:1px solid #ccc;border-radius:6px;background:#fff;cursor:pointer;font-size:.85rem}
.tabs button.active{background:#111;color:#fff;border-color:#111}
.panel{display:none}.panel.active{display:block}
.status{font-size:.8rem;margin-bottom:8px;display:flex;gap:12px;flex-wrap:wrap}
.ok{color:#16a34a}.err{color:#dc2626}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);font-size:.75rem}
th{background:#e5e7eb;padding:5px 4px;text-align:center;white-space:nowrap}
td{padding:4px;text-align:center;border-top:1px solid #e5e7eb}
td.l{text-align:left;font-weight:500}
.auto{color:#16a34a;font-weight:600}.eco{color:#2563eb;font-weight:600}.manual{color:#dc2626;font-weight:600}
.badge{display:inline-block;padding:0 4px;border-radius:3px;font-size:.65rem}
.bo{background:#fef3c7;color:#92400e}.pa{background:#fce7f3;color:#9d174d}
input[type=number]{width:58px;padding:2px 4px;font-size:.75rem;text-align:center;border:1px solid #ccc;border-radius:4px}
select{padding:2px 4px;font-size:.75rem;border:1px solid #ccc;border-radius:4px}
</style>
</head>
<body>
<h1>HCU Bridge</h1>
<div class="tabs">
<button class="active" onclick="switchTab('hcu')">HCU</button>
<button onclick="switchTab('modbus')">Modbus (SPS)</button>
</div>
<div id="tab-hcu" class="panel active">
<div class="status" id="hcu-status">Lade...</div>
<table><thead><tr>
<th>Raum</th><th>Soll</th><th>Ist</th><th>Modus</th><th>Ventil</th><th>Boost</th><th>Fenster</th><th>Fehler</th><th></th>
</tr></thead><tbody id="hcu-tbody"></tbody></table>
</div>
<div id="tab-modbus" class="panel">
<div class="status" id="mb-status">Lade...</div>
<table><thead><tr>
<th>Raum</th><th>Soll</th><th>Ist</th><th>Modus</th><th>Ventil%</th><th>Boost</th><th>Party</th><th>Fenster</th><th>Fehler</th>
</tr></thead><tbody id="mb-tbody"></tbody></table>
</div>
<script>
function switchTab(t){
 document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('active'));
 document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
 document.querySelector('.tabs button[onclick*="'+t+'"]').classList.add('active');
 document.getElementById('tab-'+t).classList.add('active');
}
async function loadHCU(){
 try{
  const r=await fetch('/api/data'); const d=await r.json();
  const s=document.getElementById('hcu-status');
  if(d.error){s.innerHTML='<span class=err>'+d.error+'</span>';return}
  const w=d.weather||{};
  s.innerHTML='<span class=ok>&#10003;</span> Wetter: '+(w.temperature||'?')+'°C '+((w.humidity||'?')+'%').padStart(2);
  document.getElementById('hcu-tbody').innerHTML=d.heating.map(r=>
   '<tr><td class=l>'+r.raum+'</td>'
   +'<td>'+r.soll_temp.toFixed(1)+'</td><td>'+r.ist_temp.toFixed(1)+'</td>'
   +'<td class='+r.modus.toLowerCase()+'>'+r.modus+'</td>'
   +'<td>'+(r.ventil*100).toFixed(0)+'%</td>'
   +'<td>'+(r.boost?'<span class="badge bo">BOOST</span>':'')+'</td>'
   +'<td'+(r.fenster==='OPEN'?' style=color:#dc2626':'')+'>'+(r.fenster==='OPEN'?'OFFEN':r.fenster||'-')+'</td>'
   +'<td>'+(r.unreach?'U ':'')+(r.lowbat?'B ':'')+(r.heating_failure?'HF ':'')+'</td>'
   +'<td><input type=number step=0.5 value='+r.soll_temp.toFixed(1)+' style=width:58px onchange="setTemp(\''+r.id+'\',this.value)">'
   +'<select onchange="setMode(\''+r.id+'\',this.value)"><option'+(r.modus==='AUTO'?' selected':'')+'>AUTO</option><option'+(r.modus==='ECO'?' selected':'')+'>ECO</option><option'+(r.modus==='MANUAL'?' selected':'')+'>MANUAL</option></select></td></tr>'
  ).join('');
 }catch(e){document.getElementById('hcu-status').innerHTML='<span class=err>Fehler</span>'}
 setTimeout(loadHCU,3000);
}
async function loadMB(){
 try{
  const r=await fetch('/api/modbus'); const d=await r.json();
  document.getElementById('mb-status').innerHTML='<span class=ok>&#10003;</span> '+(d.simulate?'SIM ':'')+d.aussentemp_c.toFixed(1)+'°C '+d.feuchte.toFixed(0)+'%';
  document.getElementById('mb-tbody').innerHTML=d.rooms.map(r=>
   '<tr><td class=l>'+r.name+'</td>'
   +'<td>'+r.soll_c.toFixed(1)+'</td><td>'+r.ist_c.toFixed(1)+'</td>'
   +'<td class='+(r.mode_raw===0?'auto':r.mode_raw===1?'eco':'manual')+'>'+r.mode+'</td>'
   +'<td>'+r.ventil_pct.toFixed(1)+'</td>'
   +'<td>'+(r.boost?'<span class="badge bo">BOOST</span>':'')+'</td>'
   +'<td>'+(r.party?'<span class="badge pa">PARTY</span>':'')+'</td>'
   +'<td>'+(r.fenster===1?'OFFEN':r.fenster===0?'ZU':'-')+'</td>'
   +'<td>'+r.fehler+'</td></tr>'
  ).join('');
 }catch(e){document.getElementById('mb-status').innerHTML='<span class=err>Fehler</span>'}
 setTimeout(loadMB,3000);
}
async function setTemp(id,v){await fetch('/api/set-temp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:id,temperature:parseFloat(v)})})}
async function setMode(id,v){await fetch('/api/set-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:id,mode:v})})}
loadHCU();loadMB();
</script>
</body>
</html>"""
