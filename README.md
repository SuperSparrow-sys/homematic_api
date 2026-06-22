# HCU Bridge

Echtzeit-Brücke zwischen einer **Homematic IP Central (HCU)** und einer **SPS per Modbus TCP**.

- HCU-Daten (Isttemp, Solltemp, Ventil, Modus, Fenster, Fehler) → Modbus Input Register
- SPS schreibt Solltemp/Modus → Modbus Holding Register → Callback → HCU per WebSocket
- Dashboard unter http://localhost:5000 (HCU / Modbus / Raw JSON)

## Hardware

- **Raspberry Pi** (getestet Pi 5, 2 GB – ~100 MB RAM im Betrieb)
- **OS**: Raspberry Pi OS Lite (Bookworm, 64-bit) oder Ubuntu Server 24.04 LTS
- Netzwerkverbindung zu HCU (192.168.0.90:6969/WSS) und SPS (Modbus TCP Port 502)

## Installation

```bash
# System aktualisieren
sudo apt update && sudo apt upgrade -y

# Python + Abhängigkeiten
sudo apt install -y python3 python3-pip python3-venv git
git clone <repo> hcu-bridge
cd hcu-bridge

# Venv + Pakete
python3 -m venv .venv
source .venv/bin/activate
pip install flask pymodbus websockets

# Port 502 erfordert Root-Rechte (Linux):
sudo setcap cap_net_bind_service=+ep $(readlink -f .venv/bin/python)
# alternativ: sudo python main.py
```

## Konfiguration

In `main.py` oben die HCU-IP anpassen:

```python
HCU_IP = "192.168.0.90"
```

Alle anderen Einstellungen (Ports, Plugins, Raumliste) sind direkt in `main.py`.

## Betrieb

```bash
python main.py
```

Erster Start fordert zur Eingabe des **HCU-Aktivierungsschlüssels** auf:
1. Dashboard http://localhost:5000 öffnen
2. Schlüssel eingeben → Token wird in `auth_token.json` gespeichert
3. `main.py` neu starten

Der Sync-Lauf aktualisiert alle 2 Sekunden die Modbus-Register aus dem HCU-Cache.

## Register-Map (Kurzreferenz)

Pro Raum (Index 0–32): Basis-Adresse = Index × 4, 4 Holding + 4 Input Register.

| Offset | Holding (HR) – SPS schreibt | Input (IR) – SPS liest |
|--------|---------------------------|------------------------|
| +0 | Solltemp ×10 (z.B. 215 = 21.5°C) | Isttemp ×10 |
| +1 | Modus 0=AUTO / 1=ECO / 2=MANUAL | Ventil 0–1000 (0.0%–100.0%) |
| +2 | Boost 0/1 | Fenster 0=ZU / 1=OFFEN / 65535=kein Sensor |
| +3 | Party 0/1 | Fehler Bit0=unreach Bit1=lowBat Bit2=heatingFailure |

Globale Register:

| Adresse | Typ | Wert |
|---------|-----|------|
| IR 0x1000 | int16 | Außentemperatur ×10 |
| IR 0x1001 | int16 | Luftfeuchte |
| IR 0x1002 | int16 | Wettercode |
| HR 0x1000 | int16 | Anzahl Räume (read-only) |
| IR 0x2000 + i | int16 | Room-ID = Index `i` (0–32) – **Raumerkennung** |

Room-ID-Prüfung durch SPS:

```
read_input_registers(0x2000 + i, 1) → muss Wert i liefern
```

## Dateien

| Datei | Zweck |
|-------|-------|
| `main.py` | Bridge-Logik (HCU-WebSocket, Modbus-Server, Sync, Dashboard) |
| `auth_token.json` | Persistierter HCU-Auth-Token (wird automatisch angelegt) |
| `templates/index.html` | Dashboard (HCU / Modbus / Raw JSON) |
| `modbus_scan.py` | Diagnose: scannt alle Register, testet Schreibbarkeit, prüft Room-IDs |
| `REGISTERMAP.md` | Vollständige Register-Dokumentation |

## Diagnose

```bash
python modbus_scan.py          # localhost:502
python modbus_scan.py 192.168.1.10 502   # Remote
```

Der Scanner listet alle Räume mit HR/IR-Werten und zeigt die Room-ID an.
Weicht die Room-ID vom Index ab, wird `!<wert>` ausgegeben.

## SPS-Seite (Siemens S7-1200/1500)

Beispiel für einen optimierten DB-Baustein (je Raum 4 HR lesen / 4 IR lesen):

- `"MB_Holding".Raum[0].Soll` = MW 0 (INT)
- `"MB_Input".Raum[0].Ist`  = IW 0 (INT)
- Raum-Index 0 = A001 (Werkstatt), Index 1 = A101 (Schleiferei), …

Vor dem Beschreiben eines HR immer die Room-ID prüfen:

```
# S7-SCL
IF "MB_Input".RoomID[7] = 7 THEN   // bestätigt Raum #7
  "MB_Holding".Raum[7].Soll := 215; // 21.5°C
END_IF;
```

## Lizenz

MIT
