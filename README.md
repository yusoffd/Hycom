# Ocean Temperature Extractor

A step-by-step web interface that pulls ocean temperature at any coordinate
(whole water column or chosen depths) from **HYCOM** or **Copernicus Marine**.
Extraction only - no prediction.

## Data sources

| Choice in step 2 | Provider | Period | Time step | Login |
|---|---|---|---|---|
| HYCOM current (ESPC-D-V02) | HYCOM | 2024-08-10 to latest date in the live feed | 3-hourly | none |
| HYCOM archived (GLBy0.08 expt_93.0) | HYCOM | 2018-12-04 to latest date in that feed | 3-hourly | none |
| HYCOM historical (GLBv0.08 expt_53.X) | HYCOM | 1994 to 2015-12-30 | 3-hourly | none |
| Copernicus GLORYS12V1 reanalysis | Copernicus Marine | 1993 onward | daily | **required** |
| Copernicus global analysis & forecast | Copernicus Marine | recent years | daily | **required** |

The default HYCOM source is ESPC-D-V02. HYCOM's public catalog lists current data
under this product; the app reads its actual timestamp coverage and excludes future
timestamps. HYCOM does not guarantee service availability or timely delivery.
The older GOFS 3.1 analysis feed is kept as an archived source. Copernicus data are
daily means (no sub-daily signal). Forecast days are never returned.

## The five steps in the app

1. **Locations** - edit the table (name, lat, lon), add a point from UTM
   coordinates, or upload a CSV. Starts with Thai Binh and Ham Rong.
2. **Dataset and dates** - pick a source and period. For Copernicus, enter your
   login here (see below).
3. **Depths and format** - whole water column or specific depths (Copernicus
   depths are matched to the nearest model level), optional bottom temperature,
   and for HYCOM 3-hourly or interpolated hourly.
4. **Check grid cells** - shows the model cell each point snaps to (coastal
   points can be land at ~8-9 km resolution), the offset, the water depth and,
   for Copernicus, the dataset's real coverage dates.
5. **Extract and download** - select one checked location to extract at a time.
   The default period is seven days; longer periods are allowed but show a
   runtime warning. Current HYCOM requests are split into daily chunks. Each
   extraction provides a quick-look chart, an **Excel workbook**, a ZIP of CSVs
   with `metadata.json`, and a CSV for the selected location.

## Copernicus Marine login

You need a Copernicus Marine account (marine.copernicus.eu). The app never
stores it:

- **Type it in step 2.** It lives only in that browser session's memory and is
  cleared by "Start over". It is not written to disk or into any download.
- **Or set it on the server** so users do not type it:
  `COPERNICUSMARINE_SERVICE_USERNAME` and `COPERNICUSMARINE_SERVICE_PASSWORD`.
  Use your host's secret store (Streamlit Community Cloud "Secrets", Cloud Run
  Secret Manager, etc.). **Never commit these to GitHub or put them in the code.**
- If the account password has ever been pasted into a chat or email, change it.

Dataset IDs are listed under "Advanced" in step 2 and can be edited. If step 4
says a dataset was not found, it lists the IDs that exist in that product.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy to the cloud

The server needs outbound HTTPS to `tds.hycom.org` (HYCOM) and/or the
Copernicus Marine services.

**Streamlit Community Cloud** (simplest)
1. Push this folder to a GitHub repo (no passwords in it).
2. At share.streamlit.io choose the repo and `app.py` as the entry point.
3. Optional: add the two Copernicus variables under the app's Secrets.

**Google Cloud Run**
```bash
gcloud run deploy ocean-extractor --source . --region asia-southeast1 \
  --memory 2Gi --timeout 3600
```
Add `--allow-unauthenticated` only if you want it public. Provide the Copernicus
login through Secret Manager rather than plain environment variables.

**Any VM / container host**
```bash
docker build -t ocean-extractor .
docker run -p 8080:8080 ocean-extractor
```

## Output

**Excel (`ocean_temperature_extract.xlsx`)**: a `Summary` sheet (requested point,
grid cell used, offset, water depth, dataset, period, row count, links to each
data sheet, and notes on units and credits) plus one sheet per location. Panes
are frozen, timestamps are formatted, and empty cells mean no data (for example
below the seabed). Excel allows 1,048,575 data rows per sheet; if a run exceeds
that the app says so and the CSV/ZIP download still works.

**CSV / ZIP**: one CSV per location: `datetime_utc`, `Te<depth>m` columns in degC
for every level with data, plus `TeBottom` and `bottom_depth_m` if enabled.
`metadata.json` records the requested point, the grid cell used, the offset, the
period, the cadence and the dataset IDs.

## Notes

- Extract locations one at a time. Periods longer than seven days can take longer;
  the app warns but does not block them.
- HYCOM latest, HYCOM historical and the Copernicus products are different model
  runs; do not mix them in one series without checking they agree.
- Copernicus `thetao` is potential temperature (degC).
- HYCOM data has no usage restrictions; credit HYCOM / NRL. Copernicus products
  must be cited as "Generated using E.U. Copernicus Marine Service Information"
  with the product DOI; the Excel notes include this.
