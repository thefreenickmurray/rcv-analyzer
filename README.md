# 🗳️ Maine 2026 Governor Primary — RCV Analyzer

A practical, short-term-use **Ranked-Choice Voting (RCV / instant-runoff)**
analyzer for Maine's 2026 Governor primaries, built for quick scenario work,
op-eds, and Lead Maine analysis. Upload town-level first-choice counts, run
Maine's instant-runoff rules, stress-test the outcome with adjustable transfer
assumptions, and export the results.

## Features

- **Upload** `.ods` / `.xlsx` / `.csv` town-level data (auto-loads the bundled
  `MEGOVprimarydata.ods` sample if nothing is uploaded).
- **Separate GOP & Dem tabs** — one per sheet detected in the workbook.
- **Standard Maine RCV** — eliminate the lowest candidate each round until one
  holds a **majority of continuing ballots**, with ballot **exhaustion**
  handling.
- **Transfer models** (since we only have first-choice data):
  - *Town proxy* — eliminated ballots flow to each town's strongest remaining
    candidate (local-preference assumption).
  - *Proportional* — split by remaining candidates' current size.
  - *Custom matrix* — you set pairwise transfer shares.
- **What-if controls** — scale each candidate's support, add uncounted/remaining
  ballots, and tune per-candidate exhaustion for sensitivity testing.
- **Visualizations** — first-round bar, vote-by-round lines, ballot-flow
  **Sankey**, round-by-round elimination table, and a town-level leader table +
  "towns led" chart.
- **Geographic heatmap** — Maine town **choropleth** colored by first-choice
  leader or by a candidate's vote share (see setup below).
- **Sensitivity analysis** — sweeps transfer models × exhaustion to show whether
  the projected winner is robust.
- **Export** round tables and town breakdowns to **CSV**.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open the local URL Streamlit prints (usually http://localhost:8501).

## Expected data shape

One sheet per primary (sheet name becomes the tab label), e.g.:

| Town  | CharlesGOP | BushGOP | MidgleyGOP | Other Candidates | Total Votes | Est. Votes Counted |
|-------|-----------:|--------:|-----------:|-----------------:|------------:|-------------------:|
| Acton |        111 |      55 |         97 |               97 |         382 |             99.00% |

The loader is forgiving: it tolerates reordered columns, `-`/blank
placeholders, percent strings, and all-zero towns. Any column that isn't
*Town*, *Total Votes*, or *Est. Votes Counted* is treated as a candidate; a
column named *Other Candidates* is handled as the minor-candidate bucket.

## Geographic heatmap setup

The choropleth needs Maine town/township boundary polygons (not bundled, to
keep the repo small). One-time setup:

1. Open the Maine GeoLibrary dataset:
   [Maine Town and Townships Boundary Polygons](https://maine.hub.arcgis.com/datasets/maine::maine-town-and-townships-boundary-polygons-feature-1/explore).
2. Click **Download → GeoJSON**.
3. Point the app at it using the sidebar's **Map boundaries** section, via any of:
   - **Bundled / local file** — save the file as `sample_data/maine_towns.geojson`.
   - **Upload GeoJSON** — upload it in the app each session.
   - **Fetch from URL** — paste the GeoJSON download link, or an ArcGIS
     `FeatureServer/0` layer URL (auto-converted to a GeoJSON query and cached).

The app auto-detects the town-name field and joins on a normalized key (so
"Cary Plt" matches "Cary Plantation"); unmatched towns (e.g. unorganized
territories) simply render blank. No Mapbox token is required — it uses the
free Carto basemap.

## Files

- `app.py` — Streamlit UI, charts, scenario controls.
- `rcv_core.py` — instant-runoff tabulator and transfer models.
- `data_io.py` — robust `.ods`/`.xlsx`/`.csv` parsing and normalization.
- `geo.py` — town-boundary GeoJSON loading, field detection, and name joins.
- `sample_data/MEGOVprimarydata.ods` — bundled reference dataset.

## Methodology & caveats

We only have **first-choice** vote counts, so 2nd/3rd preferences are
**modeled**, not observed. Outputs are *scenario projections* for analysis and
should be verified against official Secretary of State tabulations before
publication. "Other Candidates" is treated as one bucket, eliminated early with
a higher default exhaustion rate.
