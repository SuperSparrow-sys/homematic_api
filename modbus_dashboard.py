"""
Modbus-Dashboard: Zeigt alle Modbus-Register live im Browser.
Start: python modbus_dashboard.py
Dann: http://localhost:5002
"""
from flask import Flask, jsonify, render_template_string
from pymodbus.client import ModbusTcpClient
import threading, time

app = Flask(__name__)

MODBUS_HOST = "127.0.0.1"
MODBUS_PORT = 5020

cache = {"rooms": [], "global": {}, "ok": False}
cache_lock = threading.Lock()

ROOMS = [
    "A001 Werkstatt", "A101 Schleiferei", "A102 QS", "A103 Server",
    "A201 Umkleide H", "A202 IT", "A203 Vorraum", "A210 Buro",
    "A211 Buro", "A213 Besprechung", "C004 TH", "C102 Flur",
    "C103 AV", "C104 Meister", "C106 WC-D", "C107 WC",
    "C108 WC-H", "C111 Aufenthalt", "C202 Flur", "C203 Buro",
    "D003 TH", "D004 Umkleide", "D104 Besprechung", "D105 Einkauf",
    "D203 WC-D", "D204 Konstruktion", "D302 WC-H", "D303 WC-D",
    "D304 Kuche", "D305 ProjektL", "D306 Abstell", "D307 Besprechung",
    "D308 Besprechung",
]

def poll():
    while True:
        try:
            c = ModbusTcpClient(MODBUS_HOST, port=MODBUS_PORT)
            c.connect()
            rooms = []
            for i, name in enumerate(ROOMS):
                hr = c.read_holding_registers(i * 4, count=4, slave=1)
                ir = c.read_input_registers(i * 4, count=4, slave=1)
                if hr and ir:
                    rooms.append({
                        "i": i, "name": name,
                        "soll": hr.registers[0] / 10,
                        "mode": ["AUTO", "ECO", "MANUAL"][hr.registers[1]] if hr.registers[1] <= 2 else "?",
                        "mode_raw": hr.registers[1],
                        "boost": hr.registers[2],
                        "party": hr.registers[3],
                        "ist": ir.registers[0] / 10,
                        "ventil": ir.registers[1] / 10,
                        "fenster": {65535: "-", 0: "ZU", 1: "OFFEN"}.get(ir.registers[2], "?"),
                        "fehler": ir.registers[3],
                    })
            gr = c.read_input_registers(0x1000, count=3, slave=1)
            g = {"aussentemp": gr.registers[0] / 10 if gr else 0,
                 "feuchte": gr.registers[1] / 10 if gr else 0,
                 "wetter": gr.registers[2] if gr else 0}
            with cache_lock:
                cache["rooms"] = rooms
                cache["global"] = g
                cache["ok"] = True
            c.close()
        except Exception:
            with cache_lock:
                cache["ok"] = False
        time.sleep(2)

threading.Thread(target=poll, daemon=True).start()

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api")
def api():
    with cache_lock:
        return jsonify(cache)

HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Modbus Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:system-ui,sans-serif;background:#f5f5f5;color:#111;padding:20px}
h1{font-size:1.3rem;margin-bottom:8px}
.status{font-size:.85rem;margin-bottom:16px;display:flex;gap:16px;flex-wrap:wrap}
.ok{color:#16a34a}.err{color:#dc2626}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}
th{background:#e5e7eb;padding:8px 6px;font-size:.75rem;text-align:center;white-space:nowrap}
td{padding:6px;font-size:.8rem;text-align:center;border-top:1px solid #e5e7eb}
td.name{text-align:left;font-weight:500}
.auto{color:#16a34a;font-weight:600}
.eco{color:#2563eb;font-weight:600}
.manual{color:#dc2626;font-weight:600}
.offen{color:#dc2626;font-weight:600}
.badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:.7rem}
.boost-on{background:#fef3c7;color:#92400e}
.party-on{background:#fce7f3;color:#9d174d}
</style>
</head>
<body>
<h1>🏭 Modbus Dashboard</h1>
<div class="status" id="status">Verbinde...</div>
<table>
<thead><tr>
<th>Raum</th><th>Soll</th><th>Ist</th><th>Modus</th><th>Ventil</th><th>Fenster</th><th>Boost</th><th>Party</th><th>Fehler</th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
<script>
async function load(){
 try{
  const r=await fetch('/api');
  const d=await r.json();
  let html='';
  const st=document.getElementById('status');
  if(!d.ok){st.innerHTML='<span class=err>⛔ Keine Verbindung zum Modbus-Server</span>';document.getElementById('tbody').innerHTML='';return}
  st.innerHTML='<span class=ok>✅ Verbunden</span> <span>🌡️ '+d.global.aussentemp.toFixed(1)+'°C</span> <span>💧 '+d.global.feuchte.toFixed(0)+'%</span>';
  for(const room of d.rooms){
   const mc = room.mode_raw===0?'auto':room.mode_raw===1?'eco':'manual';
   html+='<tr><td class=name>'+room.name+'</td>'
    +'<td>'+room.soll.toFixed(1)+'</td>'
    +'<td>'+room.ist.toFixed(1)+'</td>'
    +'<td class='+mc+'>'+room.mode+'</td>'
    +'<td>'+room.ventil.toFixed(1)+'%</td>'
    +'<td'+(room.fenster==='OFFEN'?' class=offen':'')+'>'+room.fenster+'</td>'
    +'<td>'+(room.boost?'<span class="badge boost-on">BOOST</span>':'-')+'</td>'
    +'<td>'+(room.party?'<span class="badge party-on">PARTY</span>':'-')+'</td>'
    +'<td>'+room.fehler+'</td></tr>';
  }
  document.getElementById('tbody').innerHTML=html;
 }catch(e){document.getElementById('status').innerHTML='<span class=err>Netzwerkfehler</span>'}
}
setInterval(load,2000);load();
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=False)
