"""
Maine 2026 Governor Primary — Ranked-Choice Voting Analyzer
===========================================================

A practical Streamlit tool for simulating Maine's instant-runoff (RCV)
rules on town-level first-choice data, running "what-if" sensitivity
scenarios, and producing charts/tables suitable for advocacy and media use
(built for Lead Maine analysis).

Run locally:
    pip install -r requirements.txt
    streamlit run app.py

Because we only have *first-choice* counts, second/third preferences are
modeled with transparent, adjustable transfer assumptions. See the
"How this works / Methodology" expander in the app.
"""

from __future__ import annotations

import io
import os
from typing import Dict, List

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import geo
from data_io import PrimaryData, load_primaries
from rcv_core import RCVResult, run_irv, town_leaders

HERE = os.path.dirname(__file__)
SAMPLE_PATH = os.path.join(HERE, "sample_data", "MEGOVprimarydata.ods")
LOCAL_GEOJSON = os.path.join(HERE, "sample_data", "maine_towns.geojson")

# A stable, readable palette (Charles/Shah first => leaders get the strong blue).
PALETTE = [
    "#2563eb", "#dc2626", "#16a34a", "#9333ea",
    "#ea580c", "#0891b2", "#ca8a04", "#64748b",
]

# Default custom-transfer weights for the GOP primary (from the supplied
# matrix_default_rank_weights workbook). Outer key = eliminated ("from")
# candidate; inner = recipient ("to") -> share of ballots transferred. Each
# row's remainder (1 - sum) is treated as exhausted. Looked up by candidate
# name, so it auto-applies to the GOP sheet; other contests fall back to an
# even split.
DEFAULT_TRANSFER_WEIGHTS: Dict[str, Dict[str, float]] = {
    "CharlesGOP":       {"BushGOP": 0.10, "MidgleyGOP": 0.10, "Other Candidates": 0.20},
    "BushGOP":          {"CharlesGOP": 0.10, "MidgleyGOP": 0.50, "Other Candidates": 0.15},
    "MidgleyGOP":       {"CharlesGOP": 0.33, "BushGOP": 0.33, "Other Candidates": 0.20},
    "Other Candidates": {"CharlesGOP": 0.20, "BushGOP": 0.25, "MidgleyGOP": 0.40},
}


def default_matrix(cands: List[str]) -> pd.DataFrame:
    """Seed the transfer-matrix editor (rows = From, cols = To).

    Uses the supplied weights when every candidate is known (the GOP set);
    otherwise falls back to an even split among the other candidates.
    """
    known = all(c in DEFAULT_TRANSFER_WEIGHTS for c in cands)
    m = pd.DataFrame(0.0, index=cands, columns=cands)
    for frm in cands:
        if known:
            for to, share in DEFAULT_TRANSFER_WEIGHTS[frm].items():
                if to in m.columns:
                    m.loc[frm, to] = share
        else:
            even = round(1.0 / max(len(cands) - 1, 1), 2)
            for to in cands:
                m.loc[frm, to] = 0.0 if to == frm else even
    return m


# ==========================================================================
# Page config + light/dark theming
# ==========================================================================
st.set_page_config(
    page_title="Maine RCV Analyzer",
    page_icon="🗳️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def color_map(candidates: List[str]) -> Dict[str, str]:
    return {c: PALETTE[i % len(PALETTE)] for i, c in enumerate(candidates)}


# ==========================================================================
# Data loading (cached)
# ==========================================================================
@st.cache_data(show_spinner=False)
def _load_from_bytes(data: bytes, name: str) -> Dict[str, PrimaryData]:
    return load_primaries(data, name)


@st.cache_data(show_spinner=False)
def _load_sample() -> Dict[str, PrimaryData]:
    with open(SAMPLE_PATH, "rb") as f:
        return load_primaries(f.read(), "MEGOVprimarydata.ods")


def get_data() -> Dict[str, PrimaryData]:
    st.sidebar.header("1 · Data source")
    up = st.sidebar.file_uploader(
        "Upload town-level results (.ods, .xlsx, .csv)",
        type=["ods", "xlsx", "csv"],
        help="Use a workbook shaped like MEGOVprimarydata.ods: one sheet per "
        "primary, columns Town | Cand1 | Cand2 | Cand3 | Other Candidates | "
        "Total Votes | Est. Votes Counted.",
    )
    if up is not None:
        try:
            return _load_from_bytes(up.getvalue(), up.name)
        except Exception as exc:  # noqa: BLE001 - surface any parse error to user
            st.sidebar.error(f"Could not parse upload: {exc}")
            st.stop()

    st.sidebar.caption("No file uploaded — using bundled **MEGOVprimarydata.ods** sample.")
    try:
        return _load_sample()
    except Exception as exc:  # noqa: BLE001
        st.sidebar.error(f"Sample data unavailable: {exc}")
        st.stop()


# ==========================================================================
# Town boundary GeoJSON for the choropleth map (cached, source-flexible)
# ==========================================================================
@st.cache_data(show_spinner="Fetching town boundaries…")
def _geojson_from_url(url: str) -> dict:
    return geo.fetch_geojson(url)


@st.cache_data(show_spinner=False)
def _geojson_from_text(text: str) -> dict:
    return geo.load_geojson_text(text)


@st.cache_data(show_spinner=False)
def _geojson_from_local(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return geo.load_geojson_text(f.read())


# We don't hardcode an ArcGIS endpoint (item/org IDs change and a wrong guess
# would silently 404). Paste the dataset's "Download → GeoJSON" link, or a
# FeatureServer/MapServer layer URL (auto-converted to a GeoJSON query).
DEFAULT_BOUNDARY_URL = ""


def _resolve_boundary_url(url: str) -> str:
    """Accept either a direct GeoJSON URL or an ArcGIS layer URL."""
    u = url.strip()
    if ("FeatureServer" in u or "MapServer" in u) and "f=geojson" not in u and "/query" not in u:
        return geo.arcgis_geojson_query(u)
    return u


def get_boundaries():
    """Sidebar control to load town-boundary GeoJSON; returns dict or None."""
    st.sidebar.header("3 · Map boundaries (optional)")
    src = st.sidebar.radio(
        "Town polygons source",
        ["Bundled / local file", "Upload GeoJSON", "Fetch from URL", "Off"],
        help="Maine town boundaries for the choropleth. Get them from the "
        "ArcGIS dataset (Download → GeoJSON) and either save as "
        "sample_data/maine_towns.geojson, upload here, or paste the URL.",
    )
    try:
        if src == "Off":
            return None
        if src == "Bundled / local file":
            if os.path.exists(LOCAL_GEOJSON):
                return _geojson_from_local(LOCAL_GEOJSON)
            st.sidebar.info(
                "No `sample_data/maine_towns.geojson` found. Download it from "
                "the ArcGIS dataset (Download → GeoJSON) and save it there, or "
                "use Upload / URL."
            )
            return None
        if src == "Upload GeoJSON":
            up = st.sidebar.file_uploader("GeoJSON file", type=["geojson", "json"])
            if up is not None:
                return _geojson_from_text(up.getvalue().decode("utf-8"))
            return None
        # Fetch from URL
        raw_url = st.sidebar.text_input(
            "GeoJSON / ArcGIS layer URL", value=DEFAULT_BOUNDARY_URL,
            placeholder="Paste the dataset's Download → GeoJSON link",
            help="A direct .geojson URL, or an ArcGIS FeatureServer/MapServer "
            "layer URL (e.g. .../FeatureServer/0) which is auto-converted.",
        )
        url = _resolve_boundary_url(raw_url)
        if not url:
            st.sidebar.caption("Paste a URL, then click **Load boundaries**.")
            return None
        if st.sidebar.button("Load boundaries", use_container_width=True) or \
                st.session_state.get("_geo_loaded_url") == url:
            st.session_state["_geo_loaded_url"] = url
            return _geojson_from_url(url)
        st.sidebar.caption("Click **Load boundaries** to fetch.")
        return None
    except Exception as exc:  # noqa: BLE001
        st.sidebar.error(f"Could not load boundaries: {exc}")
        return None


# ==========================================================================
# Scenario controls (what-if) + transfer assumptions
# ==========================================================================
def scenario_controls(pdata: PrimaryData, key: str):
    """Render per-primary controls; return (adjusted_matrix, settings)."""
    cands = pdata.candidates

    with st.expander("⚙️  Transfer assumptions & methodology", expanded=False):
        st.markdown(
            "**Maine RCV rule:** eliminate the lowest candidate each round and "
            "transfer their ballots until someone holds a **majority (>50%) of "
            "continuing ballots**. We have only first-choice counts, so transfers "
            "are *modeled*:"
        )
        st.markdown(
            "- **Town proxy** — in each town, an eliminated candidate's ballots "
            "flow to that town's strongest *remaining* candidate (assumes voters "
            "rank their locally popular options).\n"
            "- **Proportional** — split among remaining candidates by their current "
            "round size.\n"
            "- **Custom matrix** — you set pairwise transfer shares below."
        )

    model_label = st.radio(
        "Transfer model",
        ["Town proxy", "Proportional", "Custom matrix"],
        horizontal=True,
        key=f"model_{key}",
        help="How an eliminated candidate's votes are reassigned each round.",
    )
    model = {"Town proxy": "town_proxy", "Proportional": "proportional", "Custom matrix": "matrix"}[
        model_label
    ]

    if pdata.other_col:
        st.caption(f"⛔ **{pdata.other_col}** is eliminated first in every "
                   "scenario — it aggregates several candidates who can't win — "
                   "and its ballots transfer per the model below.")

    if model == "matrix":
        st.caption("ℹ️ With the **Custom matrix** model, exhaustion is taken from "
                   "the matrix itself (each row's remainder), so the sliders below "
                   "are ignored.")
    st.markdown("**Ballot exhaustion** (share of an eliminated candidate's ballots "
                "that rank no one else and drop out):")
    cols = st.columns(len(cands))
    exhaustion = {}
    for i, c in enumerate(cands):
        default = 0.30 if c == pdata.other_col else 0.10
        exhaustion[c] = cols[i].slider(
            f"{c}", 0.0, 1.0, default, 0.05, key=f"ex_{key}_{c}",
            disabled=(model == "matrix"),
        )

    # ---- What-if first-round adjustments -------------------------------
    st.markdown("**What-if: adjust first-round support** (scale each candidate's "
                "town counts; e.g. model late deciders, turnout, or Other collapsing):")
    cols2 = st.columns(len(cands))
    scales = {}
    for i, c in enumerate(cands):
        scales[c] = cols2[i].slider(
            f"× {c}", 0.0, 2.0, 1.0, 0.05, key=f"sc_{key}_{c}"
        )

    extra = st.slider(
        "Add 'remaining/uncounted' ballots (distributed by current shares, %)",
        0, 50, 0, 1, key=f"extra_{key}",
        help="Inflate the electorate by this % to model ballots still to be counted.",
    )

    # ---- Custom transfer matrix editor ---------------------------------
    transfer_matrix = None
    if model == "matrix":
        st.markdown("**Custom transfer matrix** — each **row** is an eliminated "
                    "(*From*) candidate; each **column** is a recipient (*To*). "
                    "Values are the share of that candidate's ballots transferred. "
                    "Rows summing to <1 leave the remainder exhausted. Defaults to "
                    "the supplied rank-weights table.")
        transfer_matrix = st.data_editor(
            default_matrix(cands), key=f"tm_{key}", use_container_width=True
        )
        rowsums = transfer_matrix.sum(axis=1)
        if (rowsums > 1.0 + 1e-9).any():
            over = ", ".join(rowsums[rowsums > 1.0 + 1e-9].index)
            st.caption(f"⚠️ Rows over 100% will be scaled down: {over}.")

    # ---- Apply scenario to the town matrix -----------------------------
    M = pdata.df[cands].fillna(0).astype(float).copy()
    for c in cands:
        M[c] = M[c] * scales[c]
    if extra > 0:
        M = M * (1.0 + extra / 100.0)

    settings = dict(
        model=model,
        exhaustion=exhaustion,
        transfer_matrix=transfer_matrix,
        scales=scales,
        extra=extra,
    )
    return M, settings


# ==========================================================================
# Charts
# ==========================================================================
def first_round_bar(totals: pd.Series, cmap: Dict[str, str]):
    df = totals.reset_index()
    df.columns = ["Candidate", "Votes"]
    df["Share"] = (df["Votes"] / df["Votes"].sum() * 100).round(1)
    fig = px.bar(
        df.sort_values("Votes", ascending=True),
        x="Votes", y="Candidate", orientation="h",
        text=df.sort_values("Votes", ascending=True).apply(
            lambda r: f"{r['Votes']:,.0f}  ({r['Share']}%)", axis=1
        ),
        color="Candidate", color_discrete_map=cmap,
    )
    fig.update_layout(showlegend=False, height=320, margin=dict(l=10, r=10, t=30, b=10),
                      title="First-round (first-choice) totals")
    return fig


def cumulative_round_chart(result: RCVResult, cmap: Dict[str, str]):
    rows = []
    for r in result.rounds:
        for c, v in r.totals.items():
            rows.append({"Round": f"R{r.round_number}", "Candidate": c, "Votes": v})
    df = pd.DataFrame(rows)
    fig = px.line(
        df, x="Round", y="Votes", color="Candidate", markers=True,
        color_discrete_map=cmap,
    )
    fig.update_layout(height=360, margin=dict(l=10, r=10, t=40, b=10),
                      title="Vote totals by round (cumulative transfers)")
    return fig


def sankey_chart(result: RCVResult, cmap: Dict[str, str]):
    """Build a Sankey of vote flows across elimination rounds."""
    # Nodes: one (candidate, round) per active candidate per round + Exhausted.
    node_labels: List[str] = []
    node_index: Dict[tuple, int] = {}

    def node(cand: str, rnd: int) -> int:
        key = (cand, rnd)
        if key not in node_index:
            node_index[key] = len(node_labels)
            node_labels.append(f"{cand} · R{rnd}")
        return node_index[key]

    sources, targets, values, link_colors = [], [], [], []

    for idx, r in enumerate(result.rounds[:-1]):
        nxt = result.rounds[idx + 1]
        elim = r.eliminated
        # Carry-over flows: each surviving candidate keeps prior total.
        for c in r.totals:
            if c == elim:
                continue
            carry = r.totals.get(c, 0)
            if carry > 0:
                sources.append(node(c, r.round_number))
                targets.append(node(c, nxt.round_number))
                values.append(carry)
                link_colors.append(_rgba(cmap.get(c, "#94a3b8"), 0.35))
        # Transfer flows from the eliminated candidate.
        if elim:
            for to_c, moved in r.transfers.items():
                if moved > 0:
                    sources.append(node(elim, r.round_number))
                    targets.append(node(to_c, nxt.round_number))
                    values.append(moved)
                    link_colors.append(_rgba(cmap.get(elim, "#94a3b8"), 0.55))
            if r.exhausted_this_round > 0:
                ex_node = node("Exhausted", nxt.round_number)
                sources.append(node(elim, r.round_number))
                targets.append(ex_node)
                values.append(r.exhausted_this_round)
                link_colors.append("rgba(100,116,139,0.35)")

    node_colors = []
    for lbl in node_labels:
        name = lbl.split(" · ")[0]
        node_colors.append("#64748b" if name == "Exhausted" else cmap.get(name, "#94a3b8"))

    fig = go.Figure(
        go.Sankey(
            arrangement="snap",
            node=dict(label=node_labels, color=node_colors, pad=18, thickness=16,
                      line=dict(width=0)),
            link=dict(source=sources, target=targets, value=values, color=link_colors),
        )
    )
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=40, b=10),
                      title="Ballot flow across elimination rounds (Sankey)")
    return fig


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _choropleth(**kwargs):
    """Call px.choropleth_map (Plotly>=5.24) or fall back to the older
    px.choropleth_mapbox, normalizing the map-style/center keyword names."""
    if hasattr(px, "choropleth_map"):
        return px.choropleth_map(**kwargs)
    # Legacy API uses the 'mapbox' suffix and different layout kwargs.
    kwargs["mapbox_style"] = kwargs.pop("map_style")
    return px.choropleth_mapbox(**kwargs)


def town_choropleth(
    leaders: pd.DataFrame, M: pd.DataFrame, pdata: PrimaryData,
    geojson: dict, cmap: Dict[str, str], mode: str, candidate: str,
):
    """Build a Maine town choropleth.

    mode == "leader": categorical fill by first-choice town leader.
    mode == "share":  continuous fill by ``candidate``'s vote share.
    Returns (figure, match_stats) or (None, stats) if towns can't be joined.
    """
    prepared, field, stats = geo.prepare_geojson(geojson, list(leaders[pdata.town_col]))
    if prepared is None:
        return None, stats

    df = leaders.copy()
    df["join_key"] = df[pdata.town_col].map(geo.normalize_town)
    common = dict(
        geojson=prepared, locations="join_key", featureidkey="properties.join_key",
        center={"lat": 45.35, "lon": -69.2}, zoom=5.6, opacity=0.72,
        map_style="carto-positron", hover_name=pdata.town_col,
    )

    if mode == "share":
        cand_share = (M[candidate] / M[pdata.candidates].sum(axis=1).replace(0, np.nan) * 100)
        df["Share"] = cand_share.round(1).fillna(0).values
        fig = _choropleth(
            data_frame=df, color="Share",
            color_continuous_scale="Blues", range_color=(0, max(60, df["Share"].max())),
            hover_data={"join_key": False, "Total": True, "Leader": True, "Share": ":.1f"},
            **common,
        )
        title = f"{candidate} — first-choice vote share by town (%)"
    else:
        fig = _choropleth(
            data_frame=df, color="Leader", color_discrete_map=cmap,
            hover_data={"join_key": False, "Total": True, "Margin %": ":.1f"},
            **common,
        )
        title = "First-choice leader by town"

    fig.update_layout(height=560, margin=dict(l=0, r=0, t=40, b=0), title=title)
    return fig, stats


# ==========================================================================
# Per-primary rendering
# ==========================================================================
def render_primary(label: str, pdata: PrimaryData, geojson: dict = None):
    cands = pdata.candidates
    cmap = color_map(cands)

    M, settings = scenario_controls(pdata, key=label)

    # ---- Run the simulation -------------------------------------------
    force_first = [pdata.other_col] if pdata.other_col else []
    result = run_irv(
        town_matrix=M,
        candidates=cands,
        transfer_model=settings["model"],
        exhaustion=settings["exhaustion"],
        transfer_matrix=settings["transfer_matrix"],
        force_first=force_first,
    )

    totals = pd.Series({c: float(M[c].sum()) for c in cands})
    total_votes = totals.sum()

    # ---- Headline metrics ---------------------------------------------
    st.subheader(f"{label} primary — projected RCV outcome")
    leader_fr = totals.idxmax()
    mcols = st.columns(4)
    mcols[0].metric("Ballots in play", f"{total_votes:,.0f}")
    mcols[1].metric("First-round leader", leader_fr,
                    f"{totals[leader_fr] / total_votes * 100:.1f}%")
    if result.winner:
        final = result.rounds[-1]
        share = final.totals.get(result.winner, 0) / final.continuing_ballots * 100 \
            if final.continuing_ballots else 0
        mcols[2].metric("Projected RCV winner", result.winner, f"{share:.1f}% final")
        mcols[3].metric("Rounds to decide", str(len(result.rounds)))

    if result.winner and result.winner != leader_fr:
        st.warning(f"⚡ **Come-from-behind:** {result.winner} trails on first choices "
                   f"but wins after transfers under these assumptions.")
    elif result.winner:
        st.success(f"✅ {result.summary_line()}")

    # ---- Charts row ----------------------------------------------------
    c1, c2 = st.columns([1, 1])
    c1.plotly_chart(first_round_bar(totals, cmap), use_container_width=True)
    c2.plotly_chart(cumulative_round_chart(result, cmap), use_container_width=True)

    if len(result.rounds) > 1:
        st.plotly_chart(sankey_chart(result, cmap), use_container_width=True)

    # ---- Round-by-round table -----------------------------------------
    st.markdown("#### Round-by-round elimination table")
    rt = result.round_table()
    elim_note = {r.round_number: r.eliminated for r in result.rounds if r.eliminated}
    st.dataframe(
        rt.style.format("{:,.0f}", na_rep="—").background_gradient(
            cmap="Blues", axis=None
        ),
        use_container_width=True,
    )
    if elim_note:
        st.caption("Eliminated each round: "
                   + " → ".join(f"R{k}: {v}" for k, v in elim_note.items()))

    # ---- Town-level breakdown -----------------------------------------
    st.markdown("#### Town-level breakdown")
    # Recompute leaders on the *adjusted* matrix so it reflects scenarios.
    tdf = M.copy()
    tdf[pdata.town_col] = pdata.df[pdata.town_col].values
    leaders = town_leaders(tdf, cands, town_col=pdata.town_col)
    voted = leaders[leaders["Leader"] != "(no votes)"]
    lead_counts = voted["Leader"].value_counts()

    lc1, lc2 = st.columns([1, 1])
    with lc1:
        if not lead_counts.empty:
            top = lead_counts.index[0]
            st.metric(f"Towns led by {top}",
                      f"{lead_counts.iloc[0]} / {len(voted)}",
                      f"{lead_counts.iloc[0] / len(voted) * 100:.0f}% of voting towns")
        lc_df = lead_counts.rename_axis("Candidate").reset_index(name="Towns")
        bar = px.bar(lc_df, x="Towns", y="Candidate", orientation="h",
                     color="Candidate", color_discrete_map=cmap)
        bar.update_layout(showlegend=False, height=260,
                          margin=dict(l=10, r=10, t=40, b=10),
                          title="Towns led (first choice)",
                          yaxis=dict(categoryorder="total ascending"))
        st.plotly_chart(bar, use_container_width=True)
    with lc2:
        st.dataframe(
            leaders.sort_values("Total", ascending=False),
            use_container_width=True, height=320, hide_index=True,
        )

    # ---- Geographic heatmap (choropleth) ------------------------------
    st.markdown("#### Geographic heatmap")
    if geojson is None:
        st.info("Enable **Map boundaries** in the sidebar (load a Maine town "
                "GeoJSON) to see the town-level choropleth.")
    else:
        mc1, mc2 = st.columns([2, 3])
        view = mc1.radio("Color towns by", ["First-choice leader", "Candidate vote share"],
                         key=f"mapmode_{label}")
        cand_sel = mc2.selectbox("Candidate (for vote-share view)", cands,
                                 key=f"mapcand_{label}")
        mode = "leader" if view == "First-choice leader" else "share"
        try:
            fig, stats = town_choropleth(leaders, M, pdata, geojson, cmap, mode, cand_sel)
            if fig is None:
                st.warning("Couldn't match town names in the GeoJSON to the data "
                           f"(matched {stats['matched']}/{stats['total']}). Check that "
                           "the boundary file covers Maine towns.")
            else:
                st.plotly_chart(fig, use_container_width=True)
                st.caption(f"Matched {stats['matched']} of {stats['total']} towns to "
                           "polygons. Unmatched towns (e.g. unorganized territories) "
                           "appear blank.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Map rendering failed: {exc}")

    # ---- Sensitivity analysis -----------------------------------------
    with st.expander("📈 Sensitivity analysis — does the winner hold up?"):
        st.caption("Re-runs the contest across transfer models and a sweep of "
                   "Other-ballot exhaustion to test robustness of the result.")
        sens = sensitivity_table(M, pdata)
        st.dataframe(sens, use_container_width=True, hide_index=True)
        win_set = set(sens["Winner"].unique())
        if len(win_set) == 1:
            st.success(f"Robust: **{win_set.pop()}** wins in every tested scenario.")
        else:
            st.warning(f"Sensitive: winner varies across scenarios → {sorted(win_set)}")

    # ---- Export --------------------------------------------------------
    st.markdown("#### Export")
    ecols = st.columns(2)
    ecols[0].download_button(
        "⬇️ Round table (CSV)",
        rt.to_csv().encode(),
        file_name=f"{label}_rcv_rounds.csv",
        mime="text/csv",
    )
    ecols[1].download_button(
        "⬇️ Town breakdown (CSV)",
        leaders.to_csv(index=False).encode(),
        file_name=f"{label}_town_leaders.csv",
        mime="text/csv",
    )


def sensitivity_table(M: pd.DataFrame, pdata: PrimaryData) -> pd.DataFrame:
    cands = pdata.candidates
    force_first = [pdata.other_col] if pdata.other_col else []
    rows = []
    for model in ("town_proxy", "proportional"):
        for ex in (0.0, 0.2, 0.4):
            exhaustion = {c: (ex if c == pdata.other_col else 0.10) for c in cands}
            res = run_irv(M, cands, transfer_model=model, exhaustion=exhaustion,
                          force_first=force_first)
            final = res.rounds[-1] if res.rounds else None
            share = (final.totals.get(res.winner, 0) / final.continuing_ballots * 100
                     if final and final.continuing_ballots else 0)
            rows.append({
                "Transfer model": "Town proxy" if model == "town_proxy" else "Proportional",
                "Other exhaustion": f"{ex:.0%}",
                "Winner": res.winner or "—",
                "Final %": round(share, 1),
                "Rounds": len(res.rounds),
            })
    return pd.DataFrame(rows)


# ==========================================================================
# Main
# ==========================================================================
def main():
    st.title("🗳️ Maine 2026 Governor Primary — RCV Analyzer")
    st.caption("Town-level instant-runoff simulation, what-if scenarios, and "
               "round-by-round visualizations · built for Lead Maine analysis.")

    with st.expander("ℹ️  How to read this / methodology & caveats", expanded=False):
        st.markdown(
            "- Upload a workbook like **MEGOVprimarydata.ods** (one sheet per "
            "primary). With no upload, the bundled sample loads automatically.\n"
            "- We only have **first-choice** counts, so 2nd/3rd preferences are "
            "**modeled** via the selected transfer assumption. Treat outputs as "
            "*scenario projections*, not official results.\n"
            "- 'Other Candidates' is a single bucket of minor candidates who "
            "cannot win, so it is **eliminated first** and given a higher default "
            "exhaustion rate.\n"
            "- A candidate wins on holding a **majority of continuing (non-"
            "exhausted) ballots**, per Maine's rules."
        )

    data = get_data()
    if not data:
        st.error("No usable primary sheets found in the data source.")
        st.stop()

    st.sidebar.header("2 · Primaries")
    st.sidebar.caption("Each detected sheet becomes a tab below.")
    st.sidebar.markdown("\n".join(
        f"- **{k}** — {len(v.named_candidates)} candidates, "
        f"{len(v.df):,} towns" for k, v in data.items()
    ))

    geojson = get_boundaries()

    tabs = st.tabs([f"🏛️ {k}" for k in data.keys()])
    for tab, (label, pdata) in zip(tabs, data.items()):
        with tab:
            render_primary(label, pdata, geojson)

    st.divider()
    st.caption("RCV Analyzer · transfers are modeled assumptions for sensitivity "
               "testing. Verify against official Secretary of State tabulations "
               "before publication.")


if __name__ == "__main__":
    main()
