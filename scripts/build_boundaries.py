#!/usr/bin/env python3
"""
build_boundaries.py
-------------------
Turn a raw Maine GeoLibrary "Town and Townships Boundary Polygons" GeoJSON
download (~51 MB) into the compact file the app bundles at
``sample_data/maine_towns.geojson`` (~0.6 MB).

What it does:
  * dissolves the many sub-parcels per municipality into one feature per TOWN,
  * keeps only the ``TOWN`` property,
  * simplifies geometry (Douglas-Peucker, ~55 m) and rounds coordinates,
  * writes minified GeoJSON.

Usage:
    pip install shapely
    python scripts/build_boundaries.py <raw_input.geojson> [output.geojson]

Default output: sample_data/maine_towns.geojson
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

TOLERANCE = 0.0005   # ~55 m at Maine's latitude
NDECIMALS = 4        # ~11 m coordinate precision


def _round_coords(obj):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(obj[0], NDECIMALS), round(obj[1], NDECIMALS)]
        return [_round_coords(x) for x in obj]
    return obj


def build(src_path: str, out_path: str) -> None:
    with open(src_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    groups = defaultdict(list)
    for feat in data.get("features", []):
        town = (feat.get("properties") or {}).get("TOWN")
        geom = feat.get("geometry")
        if town and geom:
            try:
                groups[town].append(shape(geom))
            except Exception:  # noqa: BLE001 - skip unparseable geometry
                continue

    features = []
    for town, geoms in groups.items():
        geom = unary_union(geoms)
        if TOLERANCE:
            geom = geom.simplify(TOLERANCE, preserve_topology=True)
        if geom.is_empty:
            continue
        gj = mapping(geom)
        gj["coordinates"] = _round_coords(gj["coordinates"])
        features.append({"type": "Feature", "properties": {"TOWN": town}, "geometry": gj})

    fc = {"type": "FeatureCollection", "features": features}
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, separators=(",", ":"))

    size_mb = os.path.getsize(out_path) / 1e6
    print(f"Wrote {out_path}: {len(features)} towns, {size_mb:.2f} MB")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        here, "sample_data", "maine_towns.geojson"
    )
    build(src, dst)
