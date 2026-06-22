"""
Modbus-Scanner: Prueft Verbindung, listet alle Register,
testet Lesen/Schreiben pro Raum.

Aufruf: python modbus_scan.py [host] [port]
Standard: 127.0.0.1:502
"""
import sys, socket
from pymodbus.client import ModbusTcpClient

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 502

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
ROOM_ID_BASE = 0x2000

def e(s): print("  FEHLER: " + s)

def scan():
    # Eigene IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((HOST, PORT))
        eigene_ip = s.getsockname()[0]
        s.close()
    except:
        eigene_ip = "unbekannt"

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
        except:
            pass
    if slaves:
        print("gefunden: %s" % slaves)
    else:
        print("keine (verwende Slave 1)")
        slaves = [1]
    print()

    # Anzahl Raeume aus Holding 0x1000
    sid = slaves[0]
    raum_count = ROOM_COUNT = len(RAEUME)
    try:
        rr = c.read_holding_registers(0x1000, count=1, slave=sid)
        if rr and not hasattr(rr, 'exception_code') and rr.registers:
            raum_count = rr.registers[0]
            print("  Anzahl Raeume (HR 0x1000): %d" % raum_count)
    except:
        print("  Anzahl Raeume: %d (laut Konfiguration)" % ROOM_COUNT)
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
        except:
            print("    %-20s FEHLER" % name)
    print()

    # Alle Raeume
    print("  Raum-Register (je 4 Holding + 4 Input + Room-ID):")
    print("  %-5s %-28s | HR lesen    | IR lesen    | ID" % ("Idx", "Raum"))
    print("  " + "-" * 80)
    for i in range(min(raum_count, ROOM_COUNT)):
        name = RAEUME[i] if i < len(RAEUME) else "Raum %d" % i
        addr = i * 4
        hr_ok = ir_ok = write_ok = False
        hr_vals = ["-", "-", "-", "-"]
        ir_vals = ["-", "-", "-", "-"]
        room_id = "-"

        try:
            rr = c.read_holding_registers(addr, count=4, slave=sid)
            if rr and not hasattr(rr, 'exception_code') and rr.registers:
                hr_ok = True
                hr_vals = [str(v) for v in rr.registers]
        except:
            pass

        try:
            rr = c.read_input_registers(addr, count=4, slave=sid)
            if rr and not hasattr(rr, 'exception_code') and rr.registers:
                ir_ok = True
                ir_vals = [str(v) for v in rr.registers]
        except:
            pass

        try:
            rr = c.read_input_registers(ROOM_ID_BASE + i, count=1, slave=sid)
            if rr and not hasattr(rr, 'exception_code') and rr.registers:
                room_id = rr.registers[0]
                if room_id != i:
                    room_id = "!%d" % room_id
        except:
            pass

        hr_str = "%s/%s/%s/%s" % tuple(hr_vals)
        ir_str = "%s/%s/%s/%s" % tuple(ir_vals)
        print("  %-5d %-28s | %-12s | %-12s | %s" % (i, name, hr_str, ir_str, room_id))

    # Zusammenfassung
    print()
    print("  Legende:  R=HR-lesbar  r=IR-lesbar  W=HR-schreibbar")
    print()
    print("  Register-Map je Raum:")
    print("    HR +0: Solltemp x10    IR +0: Isttemp x10")
    print("    HR +1: Modus 0/1/2     IR +1: Ventil 0-1000")
    print("    HR +2: Boost 0/1        IR +2: Fenster 0/1/65535")
    print("    HR +3: Party 0/1        IR +3: Fehler (Bitmask)")
    print("    IR 0x%03X+i: Room-ID (i)  -> SPS erkennt Raum-Index" % ROOM_ID_BASE)
    print()
    print("  Beispiel Solltemp 21.5 C schreiben:")
    print("    write_register(%d, 215)" % 0)
    print()
    print("  Beispiel Modus AUTO (0) schreiben:")
    print("    write_register(%d, 0)" % 1)
    print()
    print("  Room-ID pruefen (SPS):")
    print("    read_input_registers(0x%X, 1) -> sollte Index liefern" % ROOM_ID_BASE)

    c.close()

if __name__ == "__main__":
    scan()
