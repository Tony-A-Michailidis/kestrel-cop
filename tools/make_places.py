#!/usr/bin/env python3
"""Build the airports and place-name layers from two public-domain datasets.

    curl -L -o airports.csv https://davidmegginson.github.io/ourairports-data/airports.csv
    curl -L -o places.geojson \
        https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_populated_places_simple.geojson
    python tools/make_places.py airports.csv places.geojson --out kestrelcop/web/static/basemap

Airports: OurAirports (public domain, ourairports.com). Large and medium airports everywhere, plus small
airports that have an IATA code and scheduled service. Each feature carries the ICAO ident, IATA code, name,
size class, municipality and country. The console labels them by ICAO at low zoom and by name close in; the
watch officer's `find_airports` tool reads the same file.

Places: Natural Earth 1:10m populated places (public domain, naturalearthdata.com), reduced to name, rank
(Natural Earth's scalerank: 0 is a megacity, 10 a village), population and capital flag. The console shows
bigger ranks first as you zoom in, and uses the layer as the label source when the basemap tiles are offline.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

AIRPORT_KINDS = {"large_airport": "large", "medium_airport": "medium", "small_airport": "small"}


def build_airports(csv_path: Path) -> dict:
    feats = []
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            kind = AIRPORT_KINDS.get(row.get("type", ""))
            if not kind:
                continue
            if kind == "small" and not (row.get("iata_code") and row.get("scheduled_service") == "yes"):
                continue
            try:
                lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
            except (TypeError, ValueError):
                continue
            props = {"icao": row.get("icao_code") or row.get("ident"), "name": row.get("name", ""), "kind": kind}
            if row.get("iata_code"):
                props["iata"] = row["iata_code"]
            if row.get("municipality"):
                props["muni"] = row["municipality"]
            if row.get("iso_country"):
                props["cc"] = row["iso_country"]
            if row.get("elevation_ft"):
                try:
                    props["elev_ft"] = int(float(row["elevation_ft"]))
                except ValueError:
                    pass
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]}})
    feats.sort(key=lambda f: ({"large": 0, "medium": 1, "small": 2}[f["properties"]["kind"]], f["properties"]["icao"]))
    return {"type": "FeatureCollection", "features": feats}


def build_places(geojson_path: Path, max_rank: int) -> dict:
    src = json.loads(geojson_path.read_text(encoding="utf-8"))
    feats = []
    for f in src.get("features", []):
        p = f.get("properties") or {}
        rank = p.get("scalerank")
        if rank is None or rank > max_rank or not p.get("name"):
            continue
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        props = {"name": p["name"], "rank": int(rank)}
        if p.get("pop_max"):
            props["pop"] = int(p["pop_max"])
        if p.get("adm0cap"):
            props["cap"] = 1
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [round(coords[0], 3), round(coords[1], 3)]}})
    feats.sort(key=lambda f: (f["properties"]["rank"], -f["properties"].get("pop", 0)))
    return {"type": "FeatureCollection", "features": feats}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("airports_csv", type=Path)
    ap.add_argument("places_geojson", type=Path)
    ap.add_argument("--out", type=Path, default=Path("kestrelcop/web/static/basemap"))
    ap.add_argument("--max-rank", type=int, default=10, help="keep Natural Earth places with scalerank <= this")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    airports = build_airports(args.airports_csv)
    places = build_places(args.places_geojson, args.max_rank)
    for name, data in (("airports.json", airports), ("places.json", places)):
        path = args.out / name
        path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        print(f"{path}: {len(data['features'])} features, {path.stat().st_size // 1024} kB")


if __name__ == "__main__":
    main()
