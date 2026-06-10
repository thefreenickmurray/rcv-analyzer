"""
geo.py
------
Loading and joining Maine town/township boundary polygons for the
choropleth map.

We deliberately keep this network-light and source-flexible. Boundaries can
come from, in priority order:

  1. A local GeoJSON file (e.g. ``sample_data/maine_towns.geojson``).
  2. A GeoJSON uploaded in the app.
  3. A URL — typically the ArcGIS "Download → GeoJSON" link from the Maine
     GeoLibrary "Maine Town and Townships Boundary Polygons" dataset, or its
     FeatureServer query endpoint.

The GeoJSON's town-name field is auto-detected by overlap with the data's
town names, and a normalized ``join_key`` property is injected onto every
feature so Plotly's ``featureidkey`` join is robust to case/spacing/"Twp"
quirks.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

# Suffixes that appear on Maine municipal names inconsistently across sources.
_SUFFIXES = (
    "town", "township", "twp", "plantation", "plt", "gore", "grant",
    "city", "of", "the",
)


def normalize_town(name: str) -> str:
    """Normalize a town name to a stable join key.

    Lowercase, strip punctuation, collapse whitespace, and drop trailing
    boilerplate suffixes ("Township", "Plt", etc.) so "Cary Plt" and
    "Cary Plantation" match.
    """
    s = re.sub(r"[^a-z0-9\s]", " ", str(name).strip().lower())
    s = re.sub(r"\s+", " ", s).strip()
    parts = s.split(" ")
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return " ".join(parts)


def fetch_geojson(url: str, timeout: int = 60) -> dict:
    """Fetch a GeoJSON document from a URL (used at runtime, not in tests)."""
    req = Request(url, headers={"User-Agent": "MaineRCVAnalyzer/1.0"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-provided URL
        return json.loads(resp.read().decode("utf-8"))


def load_geojson_text(text: str) -> dict:
    """Parse GeoJSON from a string (uploaded file or pasted text)."""
    return json.loads(text)


def _candidate_name_fields(features: List[dict]) -> List[str]:
    if not features:
        return []
    props = features[0].get("properties", {}) or {}
    return list(props.keys())


def detect_town_field(geojson: dict, town_names: List[str]) -> Optional[str]:
    """Pick the GeoJSON property whose values best overlap ``town_names``."""
    features = geojson.get("features", [])
    if not features:
        return None
    target = {normalize_town(t) for t in town_names if str(t).strip()}
    if not target:
        return None

    best_field, best_score, best_matches = None, 0.0, 0
    for field in _candidate_name_fields(features):
        vals = set()
        for f in features:
            v = (f.get("properties", {}) or {}).get(field)
            if isinstance(v, str) and v.strip():
                vals.add(normalize_town(v))
        if not vals:
            continue
        matches = len(vals & target)
        # Precision: what share of this field's values are real town names.
        # This identifies the town-name field even when the boundary file
        # covers more or fewer rows than the data.
        precision = matches / len(vals)
        if precision > best_score or (precision == best_score and matches > best_matches):
            best_field, best_score, best_matches = field, precision, matches

    # Require both a clean signal and enough absolute matches to avoid a tiny
    # or unrelated string field winning by chance.
    if best_field and best_score >= 0.3 and best_matches >= 3:
        return best_field
    return None


def prepare_geojson(
    geojson: dict, town_names: List[str]
) -> Tuple[Optional[dict], Optional[str], Dict[str, int]]:
    """Inject a normalized ``join_key`` onto every feature.

    Returns ``(prepared_geojson, town_field, stats)`` where ``stats`` reports
    how many of ``town_names`` matched a polygon. Returns ``(None, None, ...)``
    if no usable town field is found.
    """
    field = detect_town_field(geojson, town_names)
    if field is None:
        return None, None, {"matched": 0, "total": len(town_names)}

    geo_keys = set()
    for f in geojson.get("features", []):
        props = f.setdefault("properties", {})
        key = normalize_town(props.get(field, ""))
        props["join_key"] = key
        geo_keys.add(key)

    matched = sum(1 for t in town_names if normalize_town(t) in geo_keys)
    return geojson, field, {"matched": matched, "total": len(town_names)}


def arcgis_geojson_query(service_layer_url: str, out_fields: str = "*") -> str:
    """Build an ArcGIS REST → GeoJSON query URL from a FeatureServer/MapServer
    layer URL (e.g. ``.../FeatureServer/0``)."""
    base = service_layer_url.rstrip("/")
    return (
        f"{base}/query?where=1%3D1&outFields={out_fields}"
        "&returnGeometry=true&outSR=4326&f=geojson"
    )
