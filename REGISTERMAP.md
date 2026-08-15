# Modbus Register Map – HCU Bridge

TCP Port **502** (Standard), Slave-ID **1**.

Einzige Quelle der Wahrheit für Adressen/Offsets ist `registers.py` – diese
Doku muss bei Änderungen daran manuell nachgezogen werden (siehe
Analyse-Report Punkt 3.1). Es sind Register für bis zu 64 Räume vorallokiert
(`MAX_ROOMS`); die tatsächlich genutzte Anzahl steht read-only in HR 0x1000.

## Holding Register (4xxxx) – SPS lesen/schreiben

Basisadresse pro Raum = `raum_index × 4`.

| Register | Offset | Typ | Beschreibung |
|----------|--------|-----|-------------|
| 0 + 4n  | +0 | **signed** int16 | **Solltemperatur** (×10, 0.1°C) |
| 1 + 4n  | +1 | int16 | **Modus** (0=AUTO, 1=ECO, 2=MANUAL) |
| 2 + 4n  | +2 | int16 | **Boost** (0=aus, 1=ein) |
| 3 + 4n  | +3 | int16 | **Party** (0=aus, 1=ein) |

## Input Register (3xxxx) – SPS lesen

Gleiche Adressierung wie Holding.

| Register | Offset | Typ | Beschreibung |
|----------|--------|-----|-------------|
| 0 + 4n  | +0 | **signed** int16 | **Isttemperatur** (×10, 0.1°C) |
| 1 + 4n  | +1 | int16 | **Ventilposition** (0–1000 = 0.0%–100.0%) |
| 2 + 4n  | +2 | int16 | **Fenster** (0=ZU, 1=OFFEN, 65535=kein Sensor) |
| 3 + 4n  | +3 | int16 | **Fehlerstatus** (Bit0=unreach, Bit1=lowbat, Bit2=heating_failure) |

| 0x1000 (4096) | **signed** int16 | Außentemperatur (×10) |
| 0x1001 (4097) | int16 | Luftfeuchte (×10) |
| 0x1002 (4098) | int16 | Wettercode |

**Signed int16 (Zweierkomplement):** Solltemp, Isttemp und Außentemperatur
können negativ werden (Frost, unbeheizte Räume). Ein Modbus-Register selbst
ist immer 0–65535; Rohwerte über 32767 sind negativ und müssen als
`wert - 65536` interpretiert werden (siehe `from_i16()`/`to_u16()` in
`registers.py`). Bis zum Fix in diesem Report wurden negative Werte
ungeprüft geschrieben – das ließ den betroffenen Lesezugriff der SPS
komplett fehlschlagen (Timeout statt Wert), siehe Analyse-Report Punkt K1.

## Room-ID (Input Register) – Verschiebungserkennung für die SPS

`IR 0x2000 + i` enthält **nicht** den Index `i` selbst, sondern eine aus dem
Raumcode gebildete Prüfsumme (CRC32 des Codes, z. B. `A001`, maskiert auf 16
Bit – siehe `room_id()` in `registers.py`). Der Wert bleibt für denselben
Raum über Neustarts stabil, ändert sich aber tatsächlich, wenn ein Raum über
`/api/move-room` auf einen anderen Slot verschoben wird. Eine frühere Version
schrieb hier den Index `i` in sein eigenes Register – das konnte strukturell
nie einen anderen Wert als `i` enthalten und damit eine Verschiebung nie
erkennen (Analyse-Report Punkt H1).

Vor dem Beschreiben eines HR sollte die SPS die Prüfsumme aus der Tabelle
unten gegenprüfen; weicht sie ab, hat sich die Raumzuordnung verschoben und
die SPS-Konfiguration muss aktualisiert werden.

## Holding Global

| Register | Beschreibung |
|----------|-------------|
| 0x1000 | Anzahl Räume (read-only) |

## Raum-Index

Room-ID = `room_id(code)` aus `registers.py`, gültig für den aktuellen Stand
von `rooms.txt`. Ändert sich `rooms.txt`, diese Tabelle mit
`python3 -c "from registers import room_id; [print(i, r, room_id(r)) for i, r in enumerate(open('rooms.txt', encoding='utf-8').read().splitlines())]"`
neu erzeugen.

| Index | Raum | Room-ID |
|-------|------|---------|
| 0 | A001 (Werkstatt) | 13085 |
| 1 | A101 (Schleiferei) | 22826 |
| 2 | A102 (QS) | 2192 |
| 3 | A103 (Server) | 14342 |
| 4 | A201 (Umkleide Herren) | 59251 |
| 5 | A202 (IT) | 46793 |
| 6 | A203 (Vorraum) | 34399 |
| 7 | A210 (Büro) | 59044 |
| 8 | A211 (Büro) | 54834 |
| 9 | A213 (Besprechung) | 46878 |
| 10 | C004 (TH) | 3865 |
| 11 | C102 (Flur) | 49179 |
| 12 | C103 (AV) | 61581 |
| 13 | C104 (Meister) | 25902 |
| 14 | C106 (WC-D) | 1026 |
| 15 | C107 (WC) | 13460 |
| 16 | C108 (WC-H) | 10501 |
| 17 | C111 (Aufenthaltsraum) | 41184 |
| 18 | C202 (Flur) | 32322 |
| 19 | C203 (Büro) | 20180 |
| 20 | D003 (TH) | 41475 |
| 21 | D004 (Umkleide) | 14240 |
| 22 | D104 (Besprechung) | 23959 |
| 23 | D105 (Einkauf) | 27905 |
| 24 | D203 (WC-D) | 30317 |
| 25 | D204 (Konstruktion) | 58318 |
| 26 | D302 (WC-H) | 11468 |
| 27 | D303 (WC-D) | 7258 |
| 28 | D304 (Küche) | 35321 |
| 29 | D305 (Projektleitung) | 47471 |
| 30 | D306 (Abstellraum) | 59605 |
| 31 | D307 (Besprechung) | 55363 |
| 32 | D308 (Besprechung) | 50642 |
