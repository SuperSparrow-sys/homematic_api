"""
Modbus-Simulator: HCU + Raumregler-Test

Start: python modbus_simulator.py

Funktionen:
- Modbus-TCP-Server auf Port 5020 mit kompletter Register-Map
- GUI zum Simulieren von Raumtemperaturen, Fenster, Fehlern
- Simuliert den "Server" der Sollwerte schreibt (Auto->Eco etc.)
- Alle Werte live in der Tabelle
"""
import tkinter as tk
from tkinter import ttk
import threading
import random
import time

from pymodbus.server import StartTcpServer
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
from pymodbus.datastore.store import ModbusSparseDataBlock


# ─── Raum-Konfiguration ────────────────────────────────────────────

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

ROOM_COUNT = len(ROOMS)
HOLDING_ROOM_BASE = 0          # 4 Register pro Raum
INPUT_ROOM_BASE   = 0
HOLDING_GLOBAL    = 0x1000
INPUT_GLOBAL      = 0x1000


# ─── Modbus Daten-Blöcke ───────────────────────────────────────────

class HoldingBlock(ModbusSparseDataBlock):
    """Holding-Register (read/write): Sollwerte, Modi, etc."""
    def __init__(self):
        d = {}
        for i in range(ROOM_COUNT):
            a = i * 4
            d[a + 1] = 150   # Soll 15.0°C → client register 0+4i
            d[a + 2] = 1     # Modus ECO  → client register 1+4i
            d[a + 3] = 0     # Boost aus  → client register 2+4i
            d[a + 4] = 0     # Party aus  → client register 3+4i
        d[HOLDING_GLOBAL + 1] = ROOM_COUNT
        super().__init__(d)


class InputBlock(ModbusSparseDataBlock):
    """Input-Register (read-only): Ist-Temps, Ventile, Fenster, Fehler."""
    def __init__(self):
        d = {}
        for i in range(ROOM_COUNT):
            a = i * 4
            d[a + 1] = 200    # Ist 20.0°C   → client register 0+4i
            d[a + 2] = 0      # Ventil 0%    → client register 1+4i
            d[a + 3] = 65535  # Fenster kein → client register 2+4i
            d[a + 4] = 0      # Fehler 0     → client register 3+4i
        d[INPUT_GLOBAL + 1]     = 137   # Außen 13.7°C
        d[INPUT_GLOBAL + 2] = 720   # Feuchte 72%
        d[INPUT_GLOBAL + 3] = 1     # Wetter wolkig
        super().__init__(d)


# ─── GUI ───────────────────────────────────────────────────────────

SIM_CTX = None

def _hr():
    return SIM_CTX[0][0].store['h']

def _ir():
    return SIM_CTX[0][0].store['i']

def _gh(a):
    return _hr().getValues(a + 1, 1)[0]

def _gi(a):
    return _ir().getValues(a + 1, 1)[0]

def _sh(a, v):
    _hr().setValues(a + 1, [v])

def _si(a, v):
    _ir().setValues(a + 1, [v])


class SimulatorGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("HCU Modbus Simulator")
        self.root.geometry("1100x700")

        # Globale Steuerung
        top = tk.Frame(self.root)
        top.pack(fill=tk.X, padx=8, pady=4)
        tk.Label(top, text="Außentemperatur:").pack(side=tk.LEFT)
        self.outdoor_var = tk.DoubleVar(value=13.7)
        tk.Scale(top, from_=-10, to_=40, orient=tk.HORIZONTAL,
                 variable=self.outdoor_var, length=200,
                 command=lambda v: self._update_outdoor()).pack(side=tk.LEFT, padx=8)
        tk.Label(top, text="°C").pack(side=tk.LEFT)

        self.auto_btn = tk.Button(top, text="▶ AUTO simulieren (alle 3s)", command=self.toggle_auto)
        self.auto_btn.pack(side=tk.RIGHT, padx=4)
        self.auto_mode = False

        # Fenster- und Fehler-Simulation
        mid = tk.Frame(self.root)
        mid.pack(fill=tk.X, padx=8, pady=2)
        tk.Label(mid, text="Fenster-Massensimulation:").pack(side=tk.LEFT)
        tk.Button(mid, text="Alle Fenster schließen", command=self.close_all_windows).pack(side=tk.LEFT, padx=4)
        tk.Button(mid, text="Zufalls-Fenster öffnen", command=self.rand_window).pack(side=tk.LEFT, padx=4)
        tk.Button(mid, text="Alle Fehler reset", command=self.clear_errors).pack(side=tk.LEFT, padx=4)

        # Tabelle
        cols = ("Raum", "Soll", "Modus", "Ist", "Ventil%", "Fenster", "Fehler")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings", height=25)
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=100, anchor=tk.CENTER)
        self.tree.column("Raum", width=180, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        # Scrollbar
        sb = ttk.Scrollbar(self.tree, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # Doppelklick zum Schreiben von Sollwert oder Modus
        self.tree.bind("<Double-1>", self.on_double_click)

        self.running = True
        self._build_table()
        self._update_table()

        # Periodisches Update
        self.root.after(1000, self._periodic)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_table(self):
        self.tree.delete(*self.tree.get_children())
        for i, name in enumerate(ROOMS):
            self.tree.insert("", tk.END, iid=str(i), values=(name, "15.0", "ECO", "20.0", "0%", "-", "-"))

    def _update_table(self):
        if not self.running:
            return
        for i in range(ROOM_COUNT):
            addr = i * 4
            soll_raw = _gh(addr)
            mode_raw = _gh(addr + 1)
            ist_raw  = _gi(addr)
            vent_raw = _gi(addr + 1)
            win_raw  = _gi(addr + 2)
            err_raw  = _gi(addr + 3)

            soll = f"{soll_raw / 10:.1f}"
            mode = ["AUTO", "ECO", "MANUAL"][mode_raw] if 0 <= mode_raw <= 2 else "?"
            ist  = f"{ist_raw / 10:.1f}"
            vent = f"{vent_raw / 10:.1f}%"
            win  = {65535: "-", 0: "ZU", 1: "OFFEN"}.get(win_raw, f"?{win_raw}")
            err_parts = []
            if err_raw & 1: err_parts.append("unreach")
            if err_raw & 2: err_parts.append("lowbat")
            if err_raw & 4: err_parts.append("heating")
            err = ",".join(err_parts) if err_parts else "-"
            try:
                self.tree.item(str(i), values=(ROOMS[i], soll, mode, ist, vent, win, err))
            except:
                pass
        self.root.after(1000, self._update_table)

    def _periodic(self):
        if not self.running:
            return
        outdoor = int(self.outdoor_var.get() * 10)
        _si(INPUT_GLOBAL, outdoor)

        for i in range(ROOM_COUNT):
            addr = i * 4
            soll = _gh(addr)
            ist_raw = _gi(addr)
            ist = ist_raw / 10

            drift = (soll / 10 - ist) * 0.05 + random.uniform(-0.1, 0.1)
            ist += drift
            ist = max(5, min(35, ist))

            _si(addr, int(ist * 10))

            diff = soll / 10 - ist
            vent = max(0, min(1000, int(diff * 100)))
            _si(addr + 1, vent)

        self.root.after(2000, self._periodic)

    def _update_outdoor(self):
        outdoor = int(self.outdoor_var.get() * 10)
        _si(INPUT_GLOBAL, outdoor)

    def toggle_auto(self):
        """Simuliert einen 'Server' der zufällig Modi umschaltet."""
        self.auto_mode = not self.auto_mode
        self.auto_btn.config(text="⏸ AUTO pausieren" if self.auto_mode else "▶ AUTO simulieren")
        if self.auto_mode:
            self._auto_cycle()

    def _auto_cycle(self):
        if not self.auto_mode or not self.running:
            return
        i = random.randrange(ROOM_COUNT)
        addr = i * 4
        old_mode = _gh(addr + 1)
        new_mode = random.choice([m for m in [0, 1, 2] if m != old_mode])
        _sh(addr + 1, new_mode)
        if new_mode == 0:
            _sh(addr, random.choice([150, 180, 200, 210]))
        elif new_mode == 2:
            _sh(addr, random.choice([200, 220]))
        self.root.after(3000 + random.randint(0, 3000), self._auto_cycle)

    def close_all_windows(self):
        for i in range(ROOM_COUNT):
            _si(i * 4 + 2, 0)

    def rand_window(self):
        for i in range(ROOM_COUNT):
            if random.random() < 0.15:
                _si(i * 4 + 2, 1)

    def clear_errors(self):
        for i in range(ROOM_COUNT):
            _si(i * 4 + 3, 0)

    def on_double_click(self, event):
        item = self.tree.selection()[0]
        col = self.tree.identify_column(event.x)
        i = int(item)
        addr = i * 4

        if col == "#2":  # Soll (Holding)
            self._edit_value(i, addr, "Solltemperatur (Grad, z.B. 21.0)", 50, 300, lambda v: int(v * 10), is_input=False)
        elif col == "#3":  # Modus (Holding)
            self._edit_value(i, addr + 1, "Modus (0=AUTO, 1=ECO, 2=MANUAL)", 0, 2, int, is_input=False)
        elif col == "#5":  # Fenster (Input)
            self._edit_value(i, i * 4 + 2, "Fenster (0=ZU, 1=OFFEN, 65535=kein)", 0, 65535, int)

    def _edit_value(self, room, addr, prompt, vmin, vmax, convert, is_input=True):
        win = tk.Toplevel(self.root)
        win.title(f"Wert ändern - {ROOMS[room]}")
        win.geometry("350x150")
        tk.Label(win, text=prompt).pack(pady=8)
        var = tk.StringVar()
        entry = tk.Entry(win, textvariable=var, width=20)
        entry.pack(pady=4)
        entry.focus()

        def submit():
            try:
                val = convert(float(var.get()))
                val = max(vmin, min(vmax, val))
                if is_input:
                    _si(addr, val)
                else:
                    _sh(addr, val)
                win.destroy()
            except:
                pass

        tk.Button(win, text="OK", command=submit).pack(pady=8)

    def run(self):
        self.root.mainloop()

    def _on_close(self):
        self.running = False
        self.root.destroy()


# ─── Start ─────────────────────────────────────────────────────────

def start_modbus(context):
    StartTcpServer(context=context, address=("0.0.0.0", 5020))


if __name__ == "__main__":
    print("  Starte Modbus-Server auf Port 5020 ...")
    store = ModbusSlaveContext(
        di=ModbusSparseDataBlock({}),
        co=ModbusSparseDataBlock({}),
        hr=HoldingBlock(),
        ir=InputBlock(),
    )
    context = ModbusServerContext(slaves=store, single=True)
    SIM_CTX = (context,)

    t = threading.Thread(target=start_modbus, args=(context,), daemon=True)
    t.start()

    try:
        gui = SimulatorGUI()
        print("  Starte GUI ...")
        gui.run()
    except tk.TclError:
        print("  Kein Display – laufe nur als Modbus-Server (headless) ...")
        while True:
            time.sleep(10)
