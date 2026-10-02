# HYCOM Data Extractor

A step-by-step web interface that pulls ocean temperature from HYCOM GOFS 3.1
at any coordinate (whole water column or chosen depths). Extraction only - no
prediction.

## The five steps in the app

1. **Locations** - edit the table (name, lat, lon), add a point from UTM
   coordinates, or upload a CSV. Starts with Thai Binh and Ham Rong.
2. **Dataset and dates**
   - *Latest*: GOFS 3.1 analysis (GLBy0.08 expt_93.0), 2018-12-04 to today.
   - *Historical*: GOFS 3.1 reanalysis (GLBv0.08 expt_53.X), 1994 to 2015-12-30.
   - Both are 3-hourly, UTC.
3. **Depths and format** - whole water column or specific levels, optional
   bottom-temperature column, 3-hourly or interpolated hourly.
4. **Check grid cells** - shows the HYCOM cell each point snaps to (coastal
   points can be land at ~9 km resolution), the offset, and the water depth.
5. **Extract and download** - progress bar, quick-look chart, then an **Excel
   workbook** (Summary sheet + one data sheet per site), a ZIP of CSVs with
   `metadata.json`, and a CSV per site.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy to the cloud

The server needs outbound HTTPS to `tds.hycom.org`.

**Streamlit Community Cloud** (simplest)
1. Push this folder to a GitHub repo.
2. At share.streamlit.io choose the repo and `app.py` as the entry point.

**Google Cloud Run**
```bash
gcloud run deploy hycom-extractor --source . --region asia-southeast1 \
  --memory 2Gi --timeout 3600 --allow-unauthenticated
```
Drop `--allow-unauthenticated` to keep it private (IAM-protected).

**Any VM / container host**
```bash
docker build -t hycom-extractor .
docker run -p 8080:8080 hycom-extractor
```

## Output

**Excel (`hycom_extract.xlsx`)**: a `Summary` sheet (requested point, grid cell
used, offset, water depth, dataset, period, row count, links to each data sheet,
and notes on units) plus one sheet per location. Panes are frozen, timestamps are
formatted, and empty cells mean no data (for example below the seabed). Excel
allows 1,048,575 data rows per sheet; if a run exceeds that the app says so and
the CSV/ZIP download still works.

**CSV / ZIP**: one CSV per location: `datetime_utc`, `Te<depth>m` columns in degC
for every level with data, plus `TeBottom` and `bottom_depth_m` if enabled.
`metadata.json` records the requested point, the grid cell used, the offset, the
period and the cadence.

## Notes

- A long period at many points can take several minutes - the server downloads
  one point's column per time chunk. Try a short period first.
- The latest-data dataset is a different model run (2018 onward) from the
  1994-2015 reanalysis; do not mix them in one series without checking they agree.
- HYCOM data has no usage restrictions; credit HYCOM / NRL.
