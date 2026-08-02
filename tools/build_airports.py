#!/usr/bin/env python3
"""Regenerate src/skyglance/airport_data.py from OurAirports.

    python3 tools/build_airports.py

Takes the `large_airport` subset with scheduled service and an IATA code — about 1,150
rows, ~100 KB as Python. Embedding it means no 12.7 MB CSV to ship or download at
runtime. OurAirports is public domain: https://ourairports.com/data/
"""

from __future__ import annotations

import csv
import io
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://davidmegginson.github.io/ourairports-data/airports.csv"
TARGET = Path(__file__).resolve().parent.parent / "src" / "skyglance" / "airport_data.py"

HEADER = '''"""Major airports, for answering "where is the busiest traffic right now".

Generated from OurAirports (public domain, https://ourairports.com) — the
`large_airport` subset with scheduled service and an IATA code. Roughly 1,150 rows,
which is small enough to embed and removes any need to ship or download a 12.7 MB CSV.

Regenerate with tools/build_airports.py if the list ever needs refreshing.
"""

from __future__ import annotations

from typing import NamedTuple


class Airport(NamedTuple):
    icao: str
    iata: str
    name: str
    country: str
    lat: float
    lon: float


#: (icao, iata, name, country_iso2, lat, lon)
MAJOR_AIRPORTS: tuple[Airport, ...] = (
'''

FOOTER = ''')

BY_ICAO: dict[str, Airport] = {a.icao: a for a in MAJOR_AIRPORTS}
BY_IATA: dict[str, Airport] = {a.iata: a for a in MAJOR_AIRPORTS}
'''


def main() -> int:
    print(f"fetching {SOURCE} ...")
    with urllib.request.urlopen(SOURCE, timeout=120) as response:
        text = response.read().decode("utf-8")

    rows = [r for r in csv.DictReader(io.StringIO(text))
            if r["type"] == "large_airport"
            and r["scheduled_service"] == "yes"
            and r["ident"] and r["iata_code"]
            and r["latitude_deg"] and r["longitude_deg"]]
    rows.sort(key=lambda r: r["ident"])

    out = io.StringIO()
    out.write(HEADER)
    for r in rows:
        name = r["name"].replace('"', "").replace("\\", "")[:46]
        out.write(f'    Airport("{r["ident"]}", "{r["iata_code"]}", "{name}", '
                  f'"{r["iso_country"]}", {float(r["latitude_deg"]):.4f}, '
                  f'{float(r["longitude_deg"]):.4f}),\n')
    out.write(FOOTER)

    TARGET.write_text(out.getvalue())
    print(f"wrote {len(rows)} airports to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
