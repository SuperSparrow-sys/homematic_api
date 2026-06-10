# HCU Dashboard - Homematic IP

Web-Dashboard fur die Homematic IP Home Control Unit (HCU). Zeigt alle Thermostaten, Wetter, Sicherheit und Gerate

## Voraussetzungen

- Python 3.13+
- HCU im selben Netzwerk (hier: `192.168.0.90`)
- Aktivierungsschlussel aus der HCU-Weboberflache (nur fur erstmalige Authentifizierung)

## Installation

```bash
# Repository klonen
git clone <repo-url>
cd HCU-Automation

# Abhangigkeiten installieren
pip install flask websockets
```

## Starten

```bash
python main.py
```

Dashboard offnen: [http://localhost:5000](http://localhost:5000)

## Authentifizierung

Die Datei `auth_token.json` enthalt einen gultigen Auth-Token und Client-ID.
Falls der Token ablauft, muss ein neuer Aktivierungsschlussel uber die HCU-Weboberflache generiert und in der `main.py` eingetragen werden.

## API-Endpunkte

| Endpunkt | Methode | Beschreibung |
|----------|---------|-------------|
| `/` | GET | Dashboard-Oberflache |
| `/api/data` | GET | Aktuelle Systemdaten (JSON) |
| `/api/refresh` | GET | Cache manuell aktualisieren |
| `/api/set-temp` | POST | Solltemperatur setzen `{"group_id":"...","temperature":21.0}` |
| `/api/set-mode` | POST | Modus andern `{"group_id":"...","mode":"AUTO"}` (AUTO, ECO, MANUAL) |
| `/api/renew-token` | POST | Token erneuern `{"activation_key":"<KEY>"}` |

## Projektstruktur

```
main.py              - Python-Server (Flask + HCU WebSocket API)
templates/
  index.html         - Dashboard-Oberflache (HTML/JS)
auth_token.json      - Auth-Token und Client-ID
```
