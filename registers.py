"""
Zentrale Register-Layout-Definition fuer die HCU-Modbus-Bruecke.

Einzige Quelle der Wahrheit fuer Adressen/Offsets. main.py und modbus_scan.py
importieren von hier, statt die Werte jeweils eigenstaendig zu duplizieren
(siehe Analyse-Report Punkt 3.1). Aenderungen am Register-Layout muessen nur
noch hier vorgenommen werden; README.md/REGISTERMAP.md sollten bei Aenderungen
manuell nachgezogen werden.
"""
import zlib

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
OFF_SOLL  = 0   # Solltemperatur x10 (0.1 Grad), signed int16 - siehe to_u16/from_i16
OFF_MODUS = 1   # 0=AUTOMATIC, 1=ECO, 2=MANUAL
OFF_BOOST = 2   # 0=aus, 1=ein
OFF_PARTY = 3   # 0=aus, 1=ein

# Input Register (SPS liest)
OFF_IST     = 0  # Isttemperatur x10, signed int16 - siehe to_u16/from_i16
OFF_VENTIL  = 1  # Ventilposition 0-1000 (0.0-100.0%)
OFF_FENSTER = 2  # 0=ZU, 1=OFFEN, 65535=kein Sensor
OFF_FEHLER  = 3  # Bitmask: Bit0=unreach, Bit1=lowBat, Bit2=heatingFailure

MODE_NAMES = ["AUTOMATIC", "ECO", "MANUAL"]

# Globale, nicht raumbezogene Register
HOLDING_GLOBAL = 0x1000   # HR: Anzahl Raeume (read-only fuer SPS)
INPUT_GLOBAL   = 0x1000   # IR +0 Aussentemp x10 (signed int16), +1 Feuchte, +2 Wettercode
ROOM_ID_BASE   = 0x2000   # IR ROOM_ID_BASE+i = Raum-Pruefsumme fuer Slot i, siehe room_id()


# Modbus-Register sind laut Protokoll vorzeichenlose 16-Bit-Werte (0-65535).
# pymodbus kodiert jeden Registerwert beim Antworten mit struct.pack(">H",
# wert), das akzeptiert ausschliesslich 0-65535 - ein negativer Wert (z.B.
# Aussentemperatur unter 0 Grad) fuehrt sonst dazu, dass die SPS das Register
# ueberhaupt nicht mehr lesen kann (Timeout statt Wert, siehe Analyse-Report
# Punkt K1, mit isoliertem Testserver reproduziert). to_u16/from_i16 sind das
# Gegenstueckpaar fuer die klassische Zweierkomplement-Kodierung, wie sie eine
# SPS fuer ein signed INT ohnehin erwartet.
def to_u16(value):
    """Wandelt einen ggf. negativen Wert in die vorzeichenlose 16-Bit-
    Darstellung um, die ein Modbus-Register benoetigt."""
    return value & 0xFFFF


def from_i16(value):
    """Interpretiert einen Modbus-Registerwert (0-65535) als vorzeichen-
    behafteten 16-Bit-Wert zurueck - Gegenstueck zu to_u16()."""
    return value - 65536 if value > 32767 else value


def room_code(label):
    """Extrahiert den Raumcode (z.B. 'A001') aus einem rooms.txt-Eintrag wie
    'A001 (Werkstatt)'. Von main.py und modbus_scan.py gemeinsam genutzt,
    damit beide dieselbe Room-ID berechnen (siehe room_id())."""
    return label.split(" ")[0] if label else ""


def room_id(label):
    """Stabile, positionsunabhaengige Pruefsumme eines Raums fuer die
    Room-ID-Kontrolle der SPS (siehe Analyse-Report Punkt H1).

    Vorher wurde hier schlicht der laufende Modbus-Index i in sein eigenes
    Register geschrieben (write_input(ROOM_ID_BASE+i, i)) - das Register
    konnte dadurch strukturell nie einen anderen Wert als i enthalten, die
    dokumentierte SPS-seitige Pruefung "IF RoomID[7]=7" war also tautologisch
    und konnte eine tatsaechliche Verschiebung der Raumreihenfolge (z.B. per
    /api/move-room) nie erkennen.

    Stattdessen wird hier eine Pruefsumme aus dem Raumcode gebildet: Sie
    bleibt fuer denselben Raum ueber Neustarts hinweg stabil, haengt aber
    nicht von seiner aktuellen Position i ab. Wird ein Raum auf einen anderen
    Modbus-Slot verschoben, aendert sich der Wert an der alten Adresse
    tatsaechlich - eine SPS-seitige Pruefung gegen den einmalig dokumentierten
    Erwartungswert (siehe REGISTERMAP.md) kann das dann korrekt erkennen."""
    return zlib.crc32(room_code(label).encode("utf-8")) & 0xFFFF
