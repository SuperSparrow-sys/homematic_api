# Modbus Register Map – HCU Bridge

TCP Port **502** (Standard), Slave-ID **1**.

## Holding Register (4xxxx) – SPS lesen/schreiben

Basisadresse pro Raum = `raum_index × 4`.

| Register | Offset | Typ | Beschreibung |
|----------|--------|-----|-------------|
| 0 + 4n  | +0 | int16 | **Solltemperatur** (×10, 0.1°C) |
| 1 + 4n  | +1 | int16 | **Modus** (0=AUTO, 1=ECO, 2=MANUAL) |
| 2 + 4n  | +2 | int16 | **Boost** (0=aus, 1=ein) |
| 3 + 4n  | +3 | int16 | **Party** (0=aus, 1=ein) |

## Input Register (3xxxx) – SPS lesen

Gleiche Adressierung wie Holding.

| Register | Offset | Typ | Beschreibung |
|----------|--------|-----|-------------|
| 0 + 4n  | +0 | int16 | **Isttemperatur** (×10, 0.1°C) |
| 1 + 4n  | +1 | int16 | **Ventilposition** (0–1000 = 0.0%–100.0%) |
| 2 + 4n  | +2 | int16 | **Fenster** (0=ZU, 1=OFFEN, 65535=kein Sensor) |
| 3 + 4n  | +3 | int16 | **Fehlerstatus** (Bit0=unreach, Bit1=lowbat, Bit2=heating_failure) |

| 0x1000 (4096) | int16 | Außentemperatur (×10) |
| 0x1001 (4097) | int16 | Luftfeuchte (×10) |
| 0x1002 (4098) | int16 | Wettercode |

## Room-ID (Input Register) – Raum-Erkennung für SPS

## Holding Global

| Register | Beschreibung |
|----------|-------------|
| 0x1000 | Anzahl Räume (read-only) |

## Raum-Index

| Index | Raum |
|-------|------|
| 0 | A001 (Werkstatt) |
| 1 | A101 (Schleiferei) |
| 2 | A102 (QS) |
| 3 | A103 (Server) |
| 4 | A201 (Umkleide Herren) |
| 5 | A202 (IT) |
| 6 | A203 (Vorraum) |
| 7 | A210 (Büro) |
| 8 | A211 (Büro) |
| 9 | A213 (Besprechung) |
| 10 | C004 (TH) |
| 11 | C102 (Flur) |
| 12 | C103 (AV) |
| 13 | C104 (Meister) |
| 14 | C106 (WC-D) |
| 15 | C107 (WC) |
| 16 | C108 (WC-H) |
| 17 | C111 (Aufenthaltsraum) |
| 18 | C202 (Flur) |
| 19 | C203 (Büro) |
| 20 | D003 (TH) |
| 21 | D004 (Umkleide) |
| 22 | D104 (Besprechung) |
| 23 | D105 (Einkauf) |
| 24 | D203 (WC-D) |
| 25 | D204 (Konstruktion) |
| 26 | D302 (WC-H) |
| 27 | D303 (WC-D) |
| 28 | D304 (Küche) |
| 29 | D305 (Projektleitung) |
| 30 | D306 (Abstellraum) |
| 31 | D307 (Besprechung) |
| 32 | D308 (Besprechung) |
