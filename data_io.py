"""
data_io.py
----------
Robust loading and normalization of town-level Maine primary data from
.ods / .xlsx / .csv sources.

The reference workbook (MEGOVprimarydata.ods) has two sheets, "GOP" and
"DEM", each shaped like:

    Town | <Cand1> | <Cand2> | <Cand3> | Other Candidates | Total Votes | Est. Votes Counted

This module is intentionally forgiving: it tolerates messy headers,
"-" placeholders, percent strings, blank/zero towns, and re-orderable
columns, and returns a clean, typed structure the app and tabulator can
rely on.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

import pandas as pd

# Column names we treat as non-candidate metadata (matched case-insensitively
# on a normalized form).
TOWN_KEYS = {"town", "municipality", "city"}
TOTAL_KEYS = {"totalvotes", "total", "totalvote"}
COUNTED_KEYS = {"estvotescounted", "votescounted", "counted", "pctcounted", "percentcounted"}
OTHER_KEYS = {"othercandidates", "other", "otherwriteins", "writein", "writeins"}


@dataclass
class PrimaryData:
    """Normalized data for a single primary (GOP or DEM)."""

    name: str                       # e.g. "GOP"
    df: pd.DataFrame                # cleaned town-level frame (numeric cands)
    town_col: str                   # name of the town column
    candidates: List[str]           # named candidate columns (incl. "Other")
    named_candidates: List[str]     # named candidates EXCLUDING the Other bucket
    other_col: Optional[str]        # the Other/write-in column, if present
    total_col: Optional[str]        # Total Votes column, if present
    counted_col: Optional[str]      # Est. Votes Counted column, if present


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).strip().lower())


def _to_number(series: pd.Series) -> pd.Series:
    """Coerce a column to numeric, treating '-', '', 'N/A' as 0."""
    cleaned = (
        series.astype(str)
        .str.replace(",", "", regex=False)
        .str.replace("%", "", regex=False)
        .str.strip()
        .replace({"-": "0", "": "0", "n/a": "0", "N/A": "0", "nan": "0", "None": "0"})
    )
    return pd.to_numeric(cleaned, errors="coerce").fillna(0)


def _read_any(file: Union[str, io.BytesIO, bytes], name: str = "") -> Dict[str, pd.DataFrame]:
    """Read all sheets from an .ods/.xlsx, or a single frame from .csv.

    Returns a mapping of sheet name -> DataFrame.
    """
    fname = (name or getattr(file, "name", "") or "").lower()

    if isinstance(file, bytes):
        file = io.BytesIO(file)

    if fname.endswith(".csv"):
        return {"data": pd.read_csv(file)}

    engine = "odf" if fname.endswith(".ods") else None  # openpyxl auto for xlsx
    sheets = pd.read_excel(file, sheet_name=None, engine=engine)
    return sheets


def normalize_sheet(name: str, raw: pd.DataFrame) -> Optional[PrimaryData]:
    """Turn one raw sheet into a :class:`PrimaryData`, or None if unusable."""
    if raw is None or raw.empty:
        return None

    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]
    norm_map = {c: _norm(c) for c in df.columns}

    # Identify special columns.
    town_col = next((c for c, n in norm_map.items() if n in TOWN_KEYS), df.columns[0])
    total_col = next((c for c, n in norm_map.items() if n in TOTAL_KEYS), None)
    counted_col = next((c for c, n in norm_map.items() if n in COUNTED_KEYS), None)
    other_col = next((c for c, n in norm_map.items() if n in OTHER_KEYS), None)

    reserved = {town_col, total_col, counted_col}
    # Candidate columns = everything that isn't town/total/counted.
    candidate_cols = [c for c in df.columns if c not in reserved and c is not None]

    # Coerce candidate + total columns to numbers.
    for c in candidate_cols:
        df[c] = _to_number(df[c])
    if total_col:
        df[total_col] = _to_number(df[total_col])

    # Keep the counted column as a readable string/percent.
    if counted_col:
        df[counted_col] = df[counted_col].astype(str).str.strip()

    # Drop rows without a town label.
    df = df[df[town_col].astype(str).str.strip().ne("")].reset_index(drop=True)

    named = [c for c in candidate_cols if c != other_col]
    if not named:
        return None

    # Order candidates with named first, Other last (RCV display convention).
    ordered = named + ([other_col] if other_col else [])

    return PrimaryData(
        name=name,
        df=df,
        town_col=town_col,
        candidates=ordered,
        named_candidates=named,
        other_col=other_col,
        total_col=total_col,
        counted_col=counted_col,
    )


def load_primaries(
    file: Union[str, io.BytesIO, bytes], name: str = ""
) -> Dict[str, PrimaryData]:
    """Load and normalize every primary sheet found in the source file.

    Returns a dict keyed by a friendly label (the sheet name). For CSVs a
    single entry keyed "data" is returned.
    """
    sheets = _read_any(file, name)
    out: Dict[str, PrimaryData] = {}
    for sheet_name, raw in sheets.items():
        pd_obj = normalize_sheet(str(sheet_name), raw)
        if pd_obj is not None:
            out[str(sheet_name)] = pd_obj
    return out
