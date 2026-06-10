import asyncio, json, ssl, uuid, os, threading, urllib.request, urllib.error
import websockets
from flask import Flask, jsonify, render_template, request

HCU_IP = "192.168.0.90"
HCU_HOST = f"https://{HCU_IP}:6969"
HCU_WS  = f"wss://{HCU_IP}:9001"
PLUGIN_ID   = "de.jonat.pw-oertel.hcu-bridge"
PLUGIN_NAME = {"de": "PW Oertel HCU Bridge"}
TOKEN_FILE  = os.path.join(os.path.dirname(__file__), "auth_token.json")

app = Flask(__name__)
CACHE = {}
CACHE_LOCK = threading.Lock()
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE


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


async def _send_ws_command(auth_token, path, body, timeout=8):
    headers = {"authtoken": auth_token, "plugin-id": PLUGIN_ID, "hmip-system-events": "true"}
    async with websockets.connect(HCU_WS, ssl=ssl_ctx, additional_headers=headers, max_size=2**23) as ws:
        await ws.send(json.dumps({
            "id": str(uuid.uuid4()), "pluginId": PLUGIN_ID,
            "type": "PLUGIN_STATE_RESPONSE",
            "body": {"pluginReadinessStatus": "READY", "friendlyName": PLUGIN_NAME},
        }))
        await asyncio.sleep(0.3)
        req_id = str(uuid.uuid4())
        await ws.send(json.dumps({
            "id": req_id, "pluginId": PLUGIN_ID,
            "type": "HMIP_SYSTEM_REQUEST",
            "body": {"path": path, "body": body},
        }))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "HMIP_SYSTEM_RESPONSE" and msg.get("id") == req_id:
                code = msg.get("body", {}).get("code")
                if code == 200:
                    return True, "ok"
                return False, f"Fehler {code}: {msg.get('body',{}).get('body',{})}"
            if t in ("DISCOVER_REQUEST", "PLUGIN_STATE_REQUEST", "HMIP_SYSTEM_EVENT"):
                continue
            return False, f"Unerwartet: {t}"


def set_temp_sync(group_id, temperature):
    auth_token, _ = load_token()
    if not auth_token:
        return False, "Kein Auth-Token"
    return asyncio.run(_send_ws_command(auth_token, "/hmip/group/heating/setSetPointTemperature",
                                        {"groupId": group_id, "setPointTemperature": round(temperature, 1)}))

def set_mode_sync(group_id, mode):
    auth_token, _ = load_token()
    if not auth_token:
        return False, "Kein Auth-Token"
    return asyncio.run(_send_ws_command(auth_token, "/hmip/group/heating/setControlMode",
                                        {"groupId": group_id, "controlMode": mode}))


async def fetch_system_state(auth_token):
    headers = {"authtoken": auth_token, "plugin-id": PLUGIN_ID, "hmip-system-events": "false"}
    try:
        async with websockets.connect(HCU_WS, ssl=ssl_ctx, additional_headers=headers, max_size=2**23) as ws:
            await ws.send(json.dumps({
                "id": str(uuid.uuid4()), "pluginId": PLUGIN_ID,
                "type": "PLUGIN_STATE_RESPONSE",
                "body": {"pluginReadinessStatus": "READY", "friendlyName": PLUGIN_NAME},
            }))
            await asyncio.sleep(0.3)
            rid = str(uuid.uuid4())
            await ws.send(json.dumps({
                "id": rid, "pluginId": PLUGIN_ID,
                "type": "HMIP_SYSTEM_REQUEST",
                "body": {"path": "/hmip/home/getSystemState", "body": {}},
            }))
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
    if not auth_token:
        return False
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
    except:
        pass
    return False


def get_dashboard_data():
    with CACHE_LOCK:
        if not CACHE:
            return None
        d = dict(CACHE)
    devices = d.get("devices", {})
    groups  = d.get("groups", {})
    home    = d.get("home", {})

    weather = home.get("weather", {})
    heating, climate = [], []
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
                "profil": grp.get("activeProfile"),
                "min_temp": grp.get("minTemperature"),
                "max_temp": grp.get("maxTemperature"),
                "party": grp.get("partyMode"),
                "cooling": grp.get("cooling"),
                "heating_failure": grp.get("heatingFailure"),
                "unreach": grp.get("unreach"),
                "lowbat": grp.get("lowBat"),
                "dutycycle": grp.get("dutyCycle"),
                "humidity_limit": grp.get("humidityLimitValue"),
                "floor_mode": grp.get("floorHeatingMode"),
            })
        if grp.get("type") == "INDOOR_CLIMATE":
            climate.append({
                "id": gid, "raum": grp.get("label", "?"),
                "fenster": grp.get("windowState"),
                "unreach": grp.get("unreach"),
            })

    device_list, warnungen = [], []
    for did, dev in devices.items():
        label = dev.get("label", "?")
        dtype = dev.get("type", "?")
        model = dev.get("modelType", "?")
        fw    = dev.get("firmwareVersion", "?")
        reach = dev.get("permanentlyReachable", False)
        channels = dev.get("functionalChannels", {})
        ch_data = []
        for ci, ch in channels.items():
            vals = {k: v for k, v in ch.items()
                    if k not in ("functionalChannelType","label","groups","deviceId",
                                 "index","groupIndex","routerModuleSupported",
                                 "routerModuleEnabled","multicastRoutingEnabled",
                                 "configPending","filteredMulticastRoutingEnabled",
                                 "supportedOptionalFeatures")
                    and isinstance(v, (bool, int, float, str))}
            ch_data.append({"index": ci, "type": ch.get("functionalChannelType",""), "werte": vals})
            for wk in ("unreach","lowBat","sabotage","dutyCycle","deviceOverloaded",
                       "coProUpdateFailure","coProFaulty","coProRestartNeeded",
                       "deviceUndervoltage","deviceOverheated","temperatureOutOfRange",
                       "devicePowerFailureDetected"):
                if ch.get(wk) in (True, "true"):
                    warnungen.append(f"{label}: {wk}")
            if ch.get("windowState") == "OPEN":
                warnungen.append(f"Fenster offen: {label}")
        device_list.append({"id": did, "label": label, "type": dtype, "model": model, "fw": fw,
                            "reachable": reach, "channels": ch_data})

    sicherheit = {}
    for gid, grp in groups.items():
        gt = grp.get("type", "")
        if gt in ("SECURITY_ZONE",):
            sicherheit[grp.get("label", gt)] = {"active": grp.get("active"),
                                                  "silent": grp.get("silent"),
                                                  "zone": grp.get("zoneAssignmentIndex"),
                                                  "window": grp.get("windowState")}
        if gt in ("SECURITY",):
            sicherheit[grp.get("label", gt)] = {"window": grp.get("windowState")}
        if gt in ("ALARM_SWITCHING", "SECURITY_BACKUP_ALARM_SWITCHING"):
            sicherheit[grp.get("label", gt)] = {"onTime": grp.get("onTime"),
                                                  "acoustic": grp.get("signalAcoustic"),
                                                  "optical": grp.get("signalOptical")}

    func = home.get("functionalHomes", {})
    return {
        "timestamp": d.get("_last_update", ""),
        "home_id": home.get("id", ""),
        "hcu_version": home.get("currentAPVersion", ""),
        "location": home.get("location", {}),
        "weather": weather,
        "heating": sorted(heating, key=lambda x: x["raum"] or ""),
        "climate": sorted(climate, key=lambda x: x["raum"] or ""),
        "devices": device_list,
        "warnungen": warnungen,
        "sicherheit": sicherheit,
        "indoor_climate": func.get("INDOOR_CLIMATE", {}),
        "heating_count": len(heating),
        "device_count": len(device_list),
        "alarm_active": any(g.get("alarmActive") for g in [func.get("SECURITY_AND_ALARM", {})]),
    }


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/data")
def api_data():
    data = get_dashboard_data()
    if not data:
        return jsonify({"error": "Keine Daten"}), 503
    return jsonify(data)

@app.route("/api/refresh")
def api_refresh():
    return jsonify({"ok": update_cache()})

@app.route("/api/set-temp", methods=["POST"])
def api_set_temp():
    body = request.get_json(force=True)
    gid, temp = body.get("group_id"), body.get("temperature")
    if not gid or temp is None:
        return jsonify({"ok": False, "error": "group_id und temperature erforderlich"}), 400
    ok, msg = set_temp_sync(gid, float(temp))
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/set-mode", methods=["POST"])
def api_set_mode():
    body = request.get_json(force=True)
    gid, mode = body.get("group_id"), body.get("mode")
    if not gid or not mode:
        return jsonify({"ok": False, "error": "group_id und mode erforderlich"}), 400
    ok, msg = set_mode_sync(gid, mode)
    return jsonify({"ok": ok, "msg": msg})


if __name__ == "__main__":
    if update_cache():
        print(f"  Cache geladen: {len(CACHE.get('devices',{}))} Geraete")
    else:
        print("  [WARN] Konnte keine Daten laden.")
    print("  Oeffne http://localhost:5000 im Browser")
    app.run(host="0.0.0.0", port=5000, debug=False)
