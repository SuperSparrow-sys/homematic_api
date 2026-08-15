"""
Zentrale Register-Layout-Definition fuer die HCU-Modbus-Bruecke.

Einzige Quelle der Wahrheit fuer Adressen/Offsets. main.py und modbus_scan.py
importieren von hier, statt die Werte jeweils eigenstaendig zu duplizieren
(siehe Analyse-Report Punkt 3.1). Aenderungen am Register-Layout muessen nur
noch hier vorgenommen werden; README.md/REGISTERMAP.md sollten bei Aenderungen
manuell nachgezogen werden.
"""

MODBUS_PORT = 502

# Basisadresse pro Raum = Raumindex * ROOM_STRIDE
ROOM_STRIDE = 4

# Anzahl vorallokierter Raum-Slots im Modbus-Datenblock. Muss >= der real
# genutzten Raumzahl sein: ModbusSparseDataBlock beantwortet nur Adressen, die
# beim Anlegen des Blocks bereits registriert wurden (siehe validate() in
# pymodbus) - kommt zur Laufzeit ein neuer Raum hinzu, waeren dessen Register
# ohne Vorallokierung fuer die SPS nicht erreichbar (Illegal Data Address).
# ROOM_STRIDE * MAX_ROOMS muss deutlich unter HOLDING_GLOBAL/INPUT_GLOBAL bleiben.
MAX_ROOMS = 64

# Holding Register (SPS schreibt)
OFF_SOLL  = 0   # Solltemperatur x10 (0.1 Grad)
OFF_MODUS = 1   # 0=AUTOMATIC, 1=ECO, 2=MANUAL
OFF_BOOST = 2   # 0=aus, 1=ein
OFF_PARTY = 3   # 0=aus, 1=ein

# Input Register (SPS liest)
OFF_IST     = 0  # Isttemperatur x10
OFF_VENTIL  = 1  # Ventilposition 0-1000 (0.0-100.0%)
OFF_FENSTER = 2  # 0=ZU, 1=OFFEN, 65535=kein Sensor
OFF_FEHLER  = 3  # Bitmask: Bit0=unreach, Bit1=lowBat, Bit2=heatingFailure

MODE_NAMES = ["AUTOMATIC", "ECO", "MANUAL"]

# Globale, nicht raumbezogene Register
HOLDING_GLOBAL = 0x1000   # HR: Anzahl Raeume (read-only fuer SPS)
INPUT_GLOBAL   = 0x1000   # IR +0 Aussentemp x10, +1 Feuchte, +2 Wettercode
ROOM_ID_BASE   = 0x2000   # IR ROOM_ID_BASE+i = Index i, Raumerkennung fuer SPS
