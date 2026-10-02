#!/usr/bin/env python3
"""Build the offline fallback basemap from Natural Earth (public domain, naturalearthdata.com).

When the Esri basemap tiles cannot be reached (no internet, air-gapped network, blocked proxy) Kestrel still
draws coastlines, lakes and national boundaries from these files, so the picture keeps its geography.

    git clone --depth 1 --filter=blob:none --sparse https://github.com/nvkelso/natural-earth-vector
    cd natural-earth-vector && git sparse-checkout set --no-cone /geojson/ne_50m_land.geojson \
        /geojson/ne_50m_lakes.geojson /geojson/ne_50m_admin_0_boundary_lines_land.geojson
    python tools/make_basemap.py natural-earth-vector/geojson --out kestrelcop/web/static/basemap

Coordinates are rounded to 3 decimals (~100 m), which is far below the 1:50m source resolution, and
the smallest islets are dropped. The result is about a third of the size of the originals.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _round_coords(coords, nd: int):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], nd), round(coords[1], nd)]
    return [_round_coords(c, nd) for c in coords]


def _ring_area(ring) -> float:
    """Shoelace area in square degrees; good enough to drop specks."""
    a = 0.0
    for i in range(len(ring) - 1):
        a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(a) / 2.0


def _dedupe(ring):
    out = [ring[0]]
    for p in ring[1:]:
        if p != out[-1]:
            out.append(p)
    return out


def simplify(features, nd: int, min_area: float, keep_props: tuple[str, ...]) -> list[dict]:
    out = []
    for f in features:
        g = f.get("geometry") or {}
        gtype, coords = g.get("type"), g.get("coordinates")
        if not coords:
            continue
        coords = _round_coords(coords, nd)
        if gtype == "Polygon":
            polys = [coords]
        elif gtype == "MultiPolygon":
            polys = coords
        elif gtype in ("LineString", "MultiLineString"):
            lines = [coords] if gtype == "LineString" else coords
            lines = [_dedupe(l) for l in lines if len(l) >= 2]
            lines = [l for l in lines if len(l) >= 2]
            if not lines:
                continue
            geom = {"type": "MultiLineString", "coordinates": lines} if len(lines) > 1 else {"type": "LineString", "coordinates": lines[0]}
            out.append({"type": "Feature", "properties": {k: f["properties"].get(k) for k in keep_props if k in (f.get("properties") or {})}, "geometry": geom})
            continue
        else:
            continue
        kept = []
        for poly in polys:
            rings = [_dedupe(r) for r in poly]
            rings = [r for r in rings if len(r) >= 4]
            if not rings or _ring_area(rings[0]) < min_area:
                continue
            kept.append(rings)
        if not kept:
            continue
        geom = {"type": "MultiPolygon", "coordinates": kept} if len(kept) > 1 else {"type": "Polygon", "coordinates": kept[0]}
        out.append({"type": "Feature", "properties": {k: f["properties"].get(k) for k in keep_props if k in (f.get("properties") or {})}, "geometry": geom})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="directory holding the ne_50m_*.geojson files")
    ap.add_argument("--out", default="kestrelcop/web/static/basemap")
    ap.add_argument("--decimals", type=int, default=3)
    args = ap.parse_args()
    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("ne_50m_land.geojson", "land.json", 0.002, ()),
        ("ne_50m_lakes.geojson", "lakes.json", 0.02, ("name",)),
        ("ne_50m_admin_0_boundary_lines_land.geojson", "boundaries.json", 0.0, ()),
    ]
    for name, target, min_area, props in jobs:
        data = json.loads((src / name).read_text(encoding="utf-8"))
        feats = simplify(data["features"], args.decimals, min_area, props)
        text = json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":"))
        (out / target).write_text(text, encoding="utf-8")
        print(f"{target}: {len(feats)} features, {len(text) / 1024:.0f} KB (from {len(data['features'])} features, {(src / name).stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
