"""
hycom_core.py - data-extraction helpers for HYCOM GOFS 3.1 (no prediction code).

Two sources:
  analysis    GLBy0.08 expt_93.0   2018-12-04 -> present   3-hourly  (latest data)
  reanalysis  GLBv0.08 expt_53.X   1994-01-01 -> 2015-12-30  3-hourly  (history)

All times are UTC. Temperature is in degrees C (HYCOM `water_temp`).
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from typing import Callable, Optional

import numpy as np
import pandas as pd
import xarray as xr
from pyproj import Transformer

# The 40 standard HYCOM GOFS 3.1 depth levels (m).
HYCOM_LEVELS = [
    0, 2, 4, 6, 8, 10, 12, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100,
    125, 150, 200, 250, 300, 350, 400, 500, 600, 700, 800, 900, 1000, 1250,
    1500, 2000, 2500, 3000, 4000, 5000,
]

SOURCES = {
    "analysis": {
        "label": "Latest data - GOFS 3.1 analysis (GLBy0.08, expt_93.0)",
        "url": "https://tds.hycom.org/thredds/dodsC/GLBy0.08/expt_93.0",
        "start": pd.Timestamp("2018-12-04"),
        "end": None,  # present
        "chunk_days": 60,
    },
    "reanalysis": {
        "label": "Historical - GOFS 3.1 reanalysis (GLBv0.08, expt_53.X)",
        "url": "https://tds.hycom.org/thredds/dodsC/GLBv0.08/expt_53.X/data/{year}",
        "start": pd.Timestamp("1994-01-01"),
        "end": pd.Timestamp("2015-12-30"),
        "chunk_days": None,  # one file per year
    },
}

ProgressCB = Optional[Callable[[float, str], None]]


# --------------------------------------------------------------------------- #
# Coordinates
# --------------------------------------------------------------------------- #
def utm_to_latlon(easting: float, northing: float, zone: int, north: bool = True):
    """UTM (WGS84) -> (lat, lon)."""
    epsg = (32600 if north else 32700) + int(zone)
    t = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    lon, lat = t.transform(easting, northing)
    return lat, lon


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return float(2 * r * np.arcsin(np.sqrt(a)))


def wrap_lon180(lon: float) -> float:
    return ((float(lon) + 180.0) % 360.0) - 180.0


# --------------------------------------------------------------------------- #
# Dataset access
# --------------------------------------------------------------------------- #
def dataset_url(source: str, year: Optional[int] = None) -> str:
    url = SOURCES[source]["url"]
    return url.format(year=year) if "{year}" in url else url


def open_ds(url: str, retries: int = 3) -> xr.Dataset:
    last = None
    for i in range(retries):
        try:
            return xr.open_dataset(url, decode_times=True)
        except Exception as exc:  # THREDDS hiccups are common
            last = exc
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"Could not open {url}: {last}")


def _ds_lon(ds: xr.Dataset, lon: float) -> float:
    """Express lon in the dataset's convention (0-360 or -180..180)."""
    return lon % 360 if float(ds["lon"].max()) > 180 else wrap_lon180(lon)


# --------------------------------------------------------------------------- #
# Nearest ocean cell
# --------------------------------------------------------------------------- #
def probe_cell(ds: xr.Dataset, lat: float, lon: float, search: int = 3) -> dict:
    """Find the nearest grid cell with valid ocean temperature.

    Coastal sites can fall on a land cell (NaN) at 1/12 deg (~9 km), so we read a
    small window of surface temperature and take the nearest valid cell.
    Returns a dict; `ok` is False when no ocean cell is found nearby.
    """
    lon_d = _ds_lon(ds, lon)
    lats, lons = ds["lat"].values, ds["lon"].values
    ilat = int(np.abs(lats - lat).argmin())
    ilon = int(np.abs(lons - lon_d).argmin())
    la0, lo0 = max(ilat - search, 0), max(ilon - search, 0)
    la1, lo1 = min(ilat + search + 1, len(lats)), min(ilon + search + 1, len(lons))

    win = ds["water_temp"].isel(
        time=0, depth=0, lat=slice(la0, la1), lon=slice(lo0, lo1)
    ).values
    best = None
    for i in range(win.shape[0]):
        for j in range(win.shape[1]):
            if np.isnan(win[i, j]):
                continue
            d = haversine_km(lat, wrap_lon180(lon_d), lats[la0 + i],
                             wrap_lon180(lons[lo0 + j]))
            if best is None or d < best[0]:
                best = (d, la0 + i, lo0 + j)
    if best is None:
        return {"ok": False, "reason": f"no ocean cell within {search} cells"}

    d, la, lo = best
    col = ds["water_temp"].isel(time=0, lat=la, lon=lo).values
    valid = ~np.isnan(col)
    depths = ds["depth"].values
    return {
        "ok": True,
        "la": int(la),
        "lo": int(lo),
        "grid_lat": round(float(lats[la]), 4),
        "grid_lon": round(wrap_lon180(lons[lo]), 4),
        "offset_km": round(d, 2),
        "n_levels": int(valid.sum()),
        "bottom_depth_m": float(depths[np.where(valid)[0].max()]),
    }


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def _column_to_frame(da: xr.DataArray, depths, include_bottom: bool) -> pd.DataFrame:
    """da dims: (time, depth) at a single cell -> wide DataFrame."""
    full = da.load()
    all_depths = full["depth"].values
    if depths:
        keep = [d for d in all_depths if float(d) in {float(x) for x in depths}]
        sel = full.sel(depth=keep)
    else:
        sel = full
    df = sel.to_pandas()
    df.columns = [f"Te{d:g}m" for d in df.columns]
    df = df.dropna(axis=1, how="all")  # levels below the seabed

    if include_bottom:
        arr = full.values
        valid = ~np.isnan(arr)
        any_valid = valid.any(axis=1)
        last = valid.shape[1] - 1 - np.argmax(valid[:, ::-1], axis=1)
        df["TeBottom"] = np.where(any_valid, arr[np.arange(arr.shape[0]), last], np.nan)
        df["bottom_depth_m"] = np.where(any_valid, all_depths[last], np.nan)
    return df


def extract_point(
    source: str,
    cell: dict,
    t0: pd.Timestamp,
    t1: pd.Timestamp,
    depths=None,
    include_bottom: bool = True,
    hourly: bool = False,
    progress: ProgressCB = None,
) -> pd.DataFrame:
    """Extract a temperature time series (whole column or chosen levels)."""
    t0, t1 = pd.Timestamp(t0), pd.Timestamp(t1)
    la, lo = cell["la"], cell["lo"]
    frames = []

    def say(frac, msg):
        if progress:
            progress(min(max(frac, 0.0), 1.0), msg)

    if source == "analysis":
        ds = open_ds(dataset_url("analysis"))
        step = pd.Timedelta(days=SOURCES["analysis"]["chunk_days"])
        # The analysis file also holds forecast steps; never go past "now".
        now_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)
        limit = min(t1 + pd.Timedelta(hours=23), now_utc)
        starts = []
        s = t0
        while s <= limit:
            starts.append(s)
            s = s + step
        for k, s in enumerate(starts):
            e = min(s + step - pd.Timedelta(hours=3), limit)
            say(k / len(starts), f"{s.date()} -> {e.date()}")
            da = ds["water_temp"].isel(lat=la, lon=lo).sel(time=slice(s, e))
            if da.sizes["time"]:
                frames.append(_column_to_frame(da, depths, include_bottom))
        ds.close()
    else:
        years = list(range(t0.year, t1.year + 1))
        for k, y in enumerate(years):
            say(k / len(years), f"year {y}")
            ds = open_ds(dataset_url("reanalysis", y))
            da = ds["water_temp"].isel(lat=la, lon=lo).sel(
                time=slice(t0, t1 + pd.Timedelta(hours=23))
            )
            if da.sizes["time"]:
                frames.append(_column_to_frame(da, depths, include_bottom))
            ds.close()

    if not frames:
        raise RuntimeError("No data returned for that period.")
    df = pd.concat(frames)
    df = df[~df.index.duplicated()].sort_index()
    df.index.name = "datetime_utc"
    te = [c for c in df.columns if c.startswith("Te")]
    df = df.dropna(how="all", subset=te)
    if hourly:
        df = df.resample("1h").interpolate(method="time", limit=8)
    say(1.0, "done")
    return df.round(3)


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #
def build_metadata(name, lat, lon, cell, df, cfg) -> dict:
    te = [c for c in df.columns if c.startswith("Te")]
    return {
        "name": name,
        "requested_lat": lat,
        "requested_lon": lon,
        "grid_cell_lat": cell["grid_lat"],
        "grid_cell_lon": cell["grid_lon"],
        "offset_km": cell["offset_km"],
        "source": cfg["source"],
        "rows": int(len(df)),
        "start_utc": str(df.index.min()),
        "end_utc": str(df.index.max()),
        "cadence": "1 h (interpolated)" if cfg["hourly"] else "3 h (native)",
        "columns": list(df.columns),
        "non_empty_fraction": df[te].notna().mean().round(3).to_dict(),
        "median_bottom_depth_m": float(df["bottom_depth_m"].median())
        if "bottom_depth_m" in df else cell["bottom_depth_m"],
        "units": "degC",
    }


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.strip().lower()).strip("_") or "site"


EXCEL_MAX_ROWS = 1_048_575  # data rows per sheet (one row is the header)


def _sheet_name(name: str, used: set) -> str:
    """Excel sheet names: max 31 chars, none of []:*?/\\ , unique, not 'Summary'."""
    bad = set("[]:*?/\\")
    s = "".join("_" if c in bad else c for c in str(name)).strip("'") or "site"
    s = s[:31]
    base, i = s, 2
    while s.lower() in used or s.lower() == "summary":
        suffix = f"_{i}"
        s = base[: 31 - len(suffix)] + suffix
        i += 1
    used.add(s.lower())
    return s


def make_xlsx(results: dict, metas: dict) -> bytes:
    """One Excel workbook: a Summary sheet plus one data sheet per location."""
    too_long = [n for n, d in results.items() if len(d) > EXCEL_MAX_ROWS]
    if too_long:
        raise ValueError(
            "Too many rows for an Excel sheet (limit 1,048,575): " + ", ".join(too_long)
            + ". Use the CSV/ZIP download, or pick a shorter period."
        )

    used: set = set()
    sheets = {name: _sheet_name(name, used) for name in results}

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter", datetime_format="yyyy-mm-dd hh:mm") as xw:
        book = xw.book
        link = book.add_format({"font_color": "blue", "underline": 1})
        wrap = book.add_format({"text_wrap": True, "valign": "top"})
        bold = book.add_format({"bold": True})

        # ---- Summary sheet ----
        rows = []
        for name, m in metas.items():
            rows.append({
                "Site": name,
                "Data sheet": sheets[name],
                "Requested lat": m["requested_lat"],
                "Requested lon": m["requested_lon"],
                "Grid cell lat": m["grid_cell_lat"],
                "Grid cell lon": m["grid_cell_lon"],
                "Offset (km)": m["offset_km"],
                "Water depth (m)": m["median_bottom_depth_m"],
                "Dataset": SOURCES[m["source"]]["label"],
                "Start (UTC)": m["start_utc"],
                "End (UTC)": m["end_utc"],
                "Rows": m["rows"],
                "Time step": m["cadence"],
            })
        summary = pd.DataFrame(rows)
        summary.to_excel(xw, sheet_name="Summary", index=False)
        ws = xw.sheets["Summary"]
        ws.set_column(0, 0, 22)
        ws.set_column(1, 1, 22)
        ws.set_column(2, 7, 14)
        ws.set_column(8, 8, 58)
        ws.set_column(9, 10, 18)
        ws.set_column(11, 12, 16)
        for i, name in enumerate(summary["Site"], start=1):
            sn = sheets[name]
            ws.write_url(i, 1, f"internal:'{sn}'!A1", link, string=sn)

        r = len(summary) + 3
        ws.write(r, 0, "Notes", bold)
        notes = [
            "Times are UTC. Temperatures are in degrees C (HYCOM water_temp).",
            "Columns named Te<depth>m (for example Te20m) are the temperature at that depth in metres.",
            "TeBottom is the temperature at the deepest level with data; bottom_depth_m is that level's depth.",
            "A blank cell means no data (for example, a level below the seabed).",
            "Values are for the HYCOM grid cell shown above, which can sit a few km from the requested point.",
            "Source: HYCOM + NCODA Global 1/12 degree analysis/reanalysis (GOFS 3.1), hycom.org.",
        ]
        for k, text in enumerate(notes, start=1):
            ws.write(r + k, 0, text)

        # ---- one data sheet per location ----
        for name, df in results.items():
            sn = sheets[name]
            df.to_excel(xw, sheet_name=sn, freeze_panes=(1, 1))
            w = xw.sheets[sn]
            w.set_column(0, 0, 18)
            w.set_column(1, max(df.shape[1], 1), 12)
    return buf.getvalue()


def make_zip(results: dict, metas: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, df in results.items():
            z.writestr(f"{slug(name)}.csv", df.to_csv())
        z.writestr("metadata.json", json.dumps(metas, indent=2, default=str))
    return buf.getvalue()
