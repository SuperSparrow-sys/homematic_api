"""
Modbus-Scanner: Prueft Verbindung, listet alle Register,
testet Lesen/Schreiben pro Raum.

Aufruf: python modbus_scan.py [host] [port]
Standard: 127.0.0.1:502
"""
import sys, socket, os
from pymodbus.client import ModbusTcpClient
from registers import ROOM_ID_BASE, ROOM_STRIDE, MAX_ROOMS, room_id

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 502

# Raumliste wird aus rooms.txt gelesen statt hier eigenstaendig dupliziert zu
# werden (siehe Analyse-Report Punkt 3.1) - das ist die selbe Datei, aus der
# main.py die Modbus-Indizes ableitet, damit Anzeige und tatsaechliche
# Belegung nicht auseinanderlaufen koennen.
_ROOMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rooms.txt")

def _load_raeume():
    if os.path.exists(_ROOMS_FILE):
        with open(_ROOMS_FILE, encoding="utf-8") as f:
            rooms = [line.strip() for line in f if line.strip()]
        if rooms:
            return rooms
    print("  [WARN] rooms.txt nicht gefunden/leer neben modbus_scan.py - "
          "Raumnamen werden als 'Raum <i>' angezeigt.")
    return []

RAEUME = _load_raeume()

def e(s): print("  FEHLER: " + s)

def w(context, exc):
    """Loggt eine unterdrueckte Exception statt sie stillschweigend zu
    ignorieren (siehe Analyse-Report Punkt 2.5)."""
    print("  [WARN] %s: %s" % (context, exc))

def scan():
    # Eigene IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((HOST, PORT))
        eigene_ip = s.getsockname()[0]
        s.close()
    except Exception as ex:
        eigene_ip = "unbekannt"
        w("Eigene-IP-Ermittlung", ex)

    print("=" * 58)
    print("  Modbus Scanner")
    print("=" * 58)
    print("  Eigene IP:     %s" % eigene_ip)
    print("  Ziel:          %s:%d" % (HOST, PORT))
    print()

    # Verbindung
    c = ModbusTcpClient(HOST, port=PORT)
    if not c.connect():
        e("Keine Verbindung zu %s:%d" % (HOST, PORT))
        return
    print("  Verbindung:    OK")
    print()

    # Slave-IDs scannen
    print("  Suche Slave-IDs ...", end=" ")
    slaves = []
    for sid in range(1, 10):
        try:
            rr = c.read_holding_registers(0, count=1, slave=sid)
            if rr and not hasattr(rr, 'exception_code'):
                slaves.append(sid)
        except Exception as ex:
            w("Slave-Scan sid=%d" % sid, ex)
    if slaves:
        print("gefunden: %s" % slaves)
    else:
        print("keine (verwende Slave 1)")
        slaves = [1]
    print()

    # Anzahl Raeume aus Holding 0x1000, sonst aus rooms.txt, sonst MAX_ROOMS
    sid = slaves[0]
    raum_count = ROOM_COUNT = len(RAEUME) if RAEUME else MAX_ROOMS
    try:
        rr = c.read_holding_registers(0x1000, count=1, slave=sid)
        if rr and not hasattr(rr, 'exception_code') and rr.registers:
            raum_count = rr.registers[0]
            print("  Anzahl Raeume (HR 0x1000): %d" % raum_count)
    except Exception as ex:
        print("  Anzahl Raeume: %d (laut Konfiguration)" % ROOM_COUNT)
        w("Raumzahl lesen (HR 0x1000)", ex)
    print()

    # Globale Register testen
    print("  Globale Register:")
    for reg, name in [(0x1000, "HR Raumzahl"), (0x1000, "IR Aussentemp"), (0x1001, "IR Feuchte"), (0x1002, "IR Wetter")]:
        try:
            if "HR" in name:
                rr = c.read_holding_registers(reg, count=1, slave=sid)
                if rr and not hasattr(rr, 'exception_code'):
                    print("    %-20s = %s" % (name, rr.registers[0] if rr.registers else "?"))
                else:
                    print("    %-20s -" % name)
            else:
                rr = c.read_input_registers(reg, count=1, slave=sid)
                if rr and not hasattr(rr, 'exception_code'):
                    print("    %-20s = %s" % (name, rr.registers[0] if rr.registers else "?"))
                else:
                    print("    %-20s -" % name)
        except Exception as ex:
            print("    %-20s FEHLER" % name)
            w("Globales Register %s" % name, ex)
    print()

    # Alle Raeume
    print("  Raum-Register (je 4 Holding + 4 Input + Room-ID):")
    print("  %-5s %-28s | HR lesen    | IR lesen    | ID" % ("Idx", "Raum"))
    print("  " + "-" * 80)
    for i in range(min(raum_count, ROOM_COUNT)):
        name = RAEUME[i] if i < len(RAEUME) else "Raum %d" % i
        addr = i * ROOM_STRIDE
        hr_ok = ir_ok = write_ok = False
        hr_vals = ["-", "-", "-", "-"]
        ir_vals = ["-", "-", "-", "-"]

        try:
            rr = c.read_holding_registers(addr, count=4, slave=sid)
            if rr and not hasattr(rr, 'exception_code') and rr.registers:
                hr_ok = True
                hr_vals = [str(v) for v in rr.registers]
        except Exception as ex:
            w("HR lesen Raum %d" % i, ex)

        try:
            rr = c.read_input_registers(addr, count=4, slave=sid)
            if rr and not hasattr(rr, 'exception_code') and rr.registers:
                ir_ok = True
                ir_vals = [str(v) for v in rr.registers]
        except Exception as ex:
            w("IR lesen Raum %d" % i, ex)

        # Room-ID ist seit dem H1-Fix eine Pruefsumme aus dem Raumcode statt
        # des Index selbst (siehe registers.room_id()) - nur so kann eine
        # tatsaechliche Verschiebung der Raumreihenfolge erkannt werden.
        # Fuer Platzhalternamen ("Raum %d", wenn rooms.txt fehlt) gibt es
        # keinen echten Code, dann wird nur der Rohwert angezeigt.
        room_id_display = "-"
        try:
            rr = c.read_input_registers(ROOM_ID_BASE + i, count=1, slave=sid)
            if rr and not hasattr(rr, 'exception_code') and rr.registers:
                raw = rr.registers[0]
                if i < len(RAEUME):
                    expected = room_id(RAEUME[i])
                    room_id_display = str(raw) if raw == expected else "!%d (erwartet %d)" % (raw, expected)
                else:
                    room_id_display = str(raw)
        except Exception as ex:
            w("Room-ID lesen Raum %d" % i, ex)

        hr_str = "%s/%s/%s/%s" % tuple(hr_vals)
        ir_str = "%s/%s/%s/%s" % tuple(ir_vals)
        print("  %-5d %-28s | %-12s | %-12s | %s" % (i, name, hr_str, ir_str, room_id_display))

    # Zusammenfassung
    print()
    print("  Legende:  R=HR-lesbar  r=IR-lesbar  W=HR-schreibbar")
    print()
    print("  Register-Map je Raum:")
    print("    HR +0: Solltemp x10 (signed)   IR +0: Isttemp x10 (signed)")
    print("    HR +1: Modus 0/1/2              IR +1: Ventil 0-1000")
    print("    HR +2: Boost 0/1                IR +2: Fenster 0/1/65535")
    print("    HR +3: Party 0/1                IR +3: Fehler (Bitmask)")
    print("    IR 0x%03X+i: Room-Pruefsumme fuer Slot i -> SPS erkennt verschobene Raeume" % ROOM_ID_BASE)
    print("    Solltemp/Isttemp/Aussentemp sind vorzeichenbehaftet (int16,")
    print("    Zweierkomplement) - negative Rohwerte >32767 als Wert-65536 lesen.")
    print()
    print("  Beispiel Solltemp 21.5 C schreiben:")
    print("    write_register(%d, 215)" % 0)
    print()
    print("  Beispiel Modus AUTO (0) schreiben:")
    print("    write_register(%d, 0)" % 1)
    print()
    print("  Room-ID pruefen (SPS):")
    print("    read_input_registers(0x%X, 1) -> muss die in REGISTERMAP.md" % ROOM_ID_BASE)
    print("    dokumentierte Pruefsumme fuer diesen Raum liefern, sonst hat")
    print("    sich die Raumreihenfolge gegenueber der SPS-Konfiguration verschoben.")

    c.close()

if __name__ == "__main__":
    scan()
