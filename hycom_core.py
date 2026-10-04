"""
hycom_core.py - ocean-temperature extraction helpers (no prediction code).

Sources (see SOURCES):
  analysis     HYCOM GLBy0.08 expt_93.0    2018-12-04 -> present    3-hourly
  reanalysis   HYCOM GLBv0.08 expt_53.X    1994-01-01 -> 2015-12-30  3-hourly
  cmems_my     Copernicus Marine GLORYS12V1 reanalysis               daily
  cmems_anfc   Copernicus Marine global analysis & forecast          daily

All times are UTC. Temperature is in degrees C. Copernicus access needs a free
Copernicus Marine login, supplied at run time (never stored in this code).
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional, cast

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

# Variable / dimension names differ between providers.
HYCOM_NAMES = {"var": "water_temp", "lat": "lat", "lon": "lon", "depth": "depth", "time": "time"}
CMEMS_NAMES = {"var": "thetao", "lat": "latitude", "lon": "longitude", "depth": "depth", "time": "time"}

HYCOM_CREDIT = "HYCOM + NCODA Global 1/12 degree analysis/reanalysis (GOFS 3.1), hycom.org."

SOURCES = {
    "analysis": {
        "kind": "hycom",
        "names": HYCOM_NAMES,
        "label": "HYCOM latest - GOFS 3.1 analysis (GLBy0.08, expt_93.0), 3-hourly",
        "url": "https://tds.hycom.org/thredds/dodsC/GLBy0.08/expt_93.0",
        "start": pd.Timestamp("2018-12-04"),
        "end": None,  # present
        "chunk_days": 60,
        "step": "3 h",
        "credit": HYCOM_CREDIT,
    },
    "reanalysis": {
        "kind": "hycom",
        "names": HYCOM_NAMES,
        "label": "HYCOM historical - GOFS 3.1 reanalysis (GLBv0.08, expt_53.X), 3-hourly",
        "url": "https://tds.hycom.org/thredds/dodsC/GLBv0.08/expt_53.X/data/{year}",
        "start": pd.Timestamp("1994-01-01"),
        "end": pd.Timestamp("2015-12-30"),
        "chunk_days": None,  # one file per year
        "step": "3 h",
        "credit": HYCOM_CREDIT,
    },
    "cmems_my": {
        "kind": "cmems",
        "names": CMEMS_NAMES,
        "label": "Copernicus Marine - GLORYS12V1 reanalysis, daily, 1993 to present",
        "product": "GLOBAL_MULTIYEAR_PHY_001_030",
        # Two segments: the final reanalysis, then the interim extension.
        "datasets": [
            "cmems_mod_glo_phy_my_0.083deg_P1D-m",
            "cmems_mod_glo_phy_myint_0.083deg_P1D-m",
        ],
        "start": pd.Timestamp("1993-01-01"),
        "end": None,
        "step": "1 day",
        "forecast": False,
        "credit": "Generated using E.U. Copernicus Marine Service Information; "
                  "https://doi.org/10.48670/moi-00021 (GLOBAL_MULTIYEAR_PHY_001_030).",
    },
    "cmems_anfc": {
        "kind": "cmems",
        "names": CMEMS_NAMES,
        "label": "Copernicus Marine - global analysis & forecast, daily, recent years",
        "product": "GLOBAL_ANALYSISFORECAST_PHY_001_024",
        "datasets": ["cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m"],
        # Nominal bounds only; the real coverage is read from the service at the check step.
        "start": pd.Timestamp("2020-01-01"),
        "end": None,
        "step": "1 day",
        "forecast": True,
        "credit": "Generated using E.U. Copernicus Marine Service Information "
                  "(GLOBAL_ANALYSISFORECAST_PHY_001_024); see the product page for the DOI.",
    },
}

ProgressCB = Optional[Callable[[float, str], None]]


def to_ts(x) -> pd.Timestamp:
    """Convert a date/str/Timestamp to a pandas Timestamp (never NaT here)."""
    return cast(pd.Timestamp, pd.Timestamp(x))


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


def analysis_coverage() -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return the actual timestamp bounds exposed by the HYCOM analysis feed."""
    ds = open_ds(dataset_url("analysis"))
    try:
        times = pd.DatetimeIndex(ds["time"].values)
        if times.empty:
            raise RuntimeError("The HYCOM analysis dataset has no timestamps.")
        return to_ts(times[0]), to_ts(times[-1])
    finally:
        ds.close()


_UNIT_TO_PANDAS = {
    "second": "s", "seconds": "s", "minute": "min", "minutes": "min",
    "hour": "h", "hours": "h", "day": "D", "days": "D",
}


def decode_time_manually(ds: xr.Dataset, year: Optional[int] = None) -> xr.Dataset:
    """Decode a `time` axis that xarray cannot decode on its own.

    HYCOM's GOFS 3.1 reanalysis yearly files (expt_53.X) store time as
    "hours since analysis" - not a real date - and put the real start in a
    separate `time_origin` attribute. We rebuild the timestamps from that
    attribute. If it is missing, `year` (the file's year) is used as a fallback.
    """
    t = ds["time"]
    units = str(t.attrs.get("units", ""))
    m = re.match(r"\s*(\w+)\s+since\s+(.*)", units, re.IGNORECASE)
    unit_name = m.group(1).lower() if m else "hours"
    if unit_name not in _UNIT_TO_PANDAS:
        raise ValueError(f"Unsupported time unit {unit_name!r} in {units!r}")

    candidates = [m.group(2).strip()] if m else []
    for key in ("time_origin", "origin", "base_date", "time_reference"):
        if key in t.attrs:
            candidates.append(str(t.attrs[key]))
    origin = None
    for text in candidates:
        try:
            ts = pd.Timestamp(text)
        except Exception:
            continue
        if not pd.isna(ts):
            origin = cast(pd.Timestamp, ts)
            break
    if origin is None and year is not None:
        origin = pd.Timestamp(year=int(year), month=1, day=1)
    if origin is None:
        raise ValueError(f"Cannot work out the time origin from units {units!r}")

    values = np.asarray(t.values, dtype="float64")
    idx = origin + pd.to_timedelta(values, unit=cast(Any, _UNIT_TO_PANDAS[unit_name]))
    return ds.assign_coords(time=pd.DatetimeIndex(idx))


def open_ds(url: str, retries: int = 3, year: Optional[int] = None) -> xr.Dataset:
    """Open an OPeNDAP/netCDF dataset, coping with HYCOM's odd time units."""
    last = None
    for i in range(retries):
        raw = None
        try:
            raw = xr.open_dataset(url, decode_cf=False)
            if "tau" in raw.variables and raw["tau"].attrs.get("units") == "hours since analysis":
                raw["tau"].attrs.pop("units")
            try:
                return xr.decode_cf(raw)
            except ValueError as exc:
                # HYCOM historical files use "hours since analysis" for time.
                if "decode" not in str(exc).lower():
                    raise
                return decode_time_manually(raw, year=year)
        except Exception as exc:  # THREDDS hiccups are common
            if raw is not None:
                raw.close()
            last = exc
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"Could not open {url}: {last}")


def _ds_lon(ds: xr.Dataset, lon: float, names: dict = HYCOM_NAMES) -> float:
    """Express lon in the dataset's convention (0-360 or -180..180)."""
    return lon % 360 if float(ds[names["lon"]].max()) > 180 else wrap_lon180(lon)


# --------------------------------------------------------------------------- #
# Nearest ocean cell
# --------------------------------------------------------------------------- #
def probe_cell(ds: xr.Dataset, lat: float, lon: float, search: int = 3,
               names: dict = HYCOM_NAMES) -> dict:
    """Find the nearest grid cell with valid ocean temperature.

    Coastal sites can fall on a land cell (NaN) at 1/12 deg (~9 km), so we read a
    small window of surface temperature and take the nearest valid cell.
    Returns a dict; `ok` is False when no ocean cell is found nearby.
    """
    n = names
    lon_d = _ds_lon(ds, lon, n)
    lats, lons = ds[n["lat"]].values, ds[n["lon"]].values
    ilat = int(np.abs(lats - lat).argmin())
    ilon = int(np.abs(lons - lon_d).argmin())
    la0, lo0 = max(ilat - search, 0), max(ilon - search, 0)
    la1, lo1 = min(ilat + search + 1, len(lats)), min(ilon + search + 1, len(lons))

    win = ds[n["var"]].isel(
        {n["time"]: 0, n["depth"]: 0, n["lat"]: slice(la0, la1), n["lon"]: slice(lo0, lo1)}
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
    col = ds[n["var"]].isel({n["time"]: 0, n["lat"]: la, n["lon"]: lo}).values
    valid = ~np.isnan(col)
    depths = ds[n["depth"]].values
    return {
        "ok": True,
        "la": int(la),
        "lo": int(lo),
        "grid_lat": round(float(lats[la]), 4),
        "grid_lon": round(wrap_lon180(lons[lo]), 4),
        "lat_label": float(lats[la]),   # exact coordinate labels in the dataset's own convention
        "lon_label": float(lons[lo]),
        "offset_km": round(d, 2),
        "n_levels": int(valid.sum()),
        "bottom_depth_m": float(depths[np.where(valid)[0].max()]),
    }


# --------------------------------------------------------------------------- #
# Copernicus Marine access
# --------------------------------------------------------------------------- #
class CmemsAuthError(RuntimeError):
    """Login problem. Never retried, to avoid repeated failed sign-ins."""


Creds = Optional[dict]  # {"username": ..., "password": ...} or None


def _have_login(creds: Creds) -> bool:
    if creds and creds.get("username") and creds.get("password"):
        return True
    if os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME") and os.environ.get(
        "COPERNICUSMARINE_SERVICE_PASSWORD"
    ):
        return True
    return (Path.home() / ".copernicusmarine" / ".copernicusmarine-credentials").exists()


have_login = _have_login  # public name used by the app


def _looks_like_auth_problem(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in ("username", "password", "credential", "authenticat", "401", "unauthor"))


def source_datasets(source: str, custom_ids: str = "") -> list:
    """Dataset IDs to use for a Copernicus source (optionally user-overridden)."""
    ids = [x.strip() for x in (custom_ids or "").split(",") if x.strip()]
    return ids or list(SOURCES[source].get("datasets", []))


def open_cmems(dataset_id: str, lat: float, lon: float, creds: Creds = None,
               box: float = 0.5, retries: int = 2) -> xr.Dataset:
    """Lazily open a small box (+/- `box` degrees) of a Copernicus Marine dataset.

    The login is passed explicitly for this call only; nothing is written to disk.
    Without any login the toolbox would wait for terminal input (hanging a web
    app), so we stop first with a clear message.
    """
    if not _have_login(creds):
        raise CmemsAuthError(
            "No Copernicus Marine login. Enter your username and password in step 2 "
            "(or set COPERNICUSMARINE_SERVICE_USERNAME and COPERNICUSMARINE_SERVICE_PASSWORD "
            "on the server)."
        )
    import copernicusmarine  # imported here so HYCOM-only use does not need it

    lon_c = wrap_lon180(lon)
    kwargs: dict = dict(
        dataset_id=dataset_id,
        variables=[CMEMS_NAMES["var"]],
        minimum_longitude=lon_c - box, maximum_longitude=lon_c + box,
        minimum_latitude=lat - box, maximum_latitude=lat + box,
    )
    if creds and creds.get("username") and creds.get("password"):
        kwargs["username"] = creds["username"]
        kwargs["password"] = creds["password"]

    last: Optional[Exception] = None
    for i in range(retries):
        try:
            return copernicusmarine.open_dataset(**kwargs)
        except Exception as exc:
            if _looks_like_auth_problem(str(exc)):
                raise CmemsAuthError(
                    "Copernicus Marine rejected the login. Check your username and password "
                    f"(details: {str(exc)[:160]})"
                ) from None
            last = exc
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"Could not open Copernicus dataset {dataset_id}: {str(last)[:300]}")


def list_cmems_datasets(product_id: str) -> list:
    """Dataset IDs that really exist in a Copernicus product (no login needed)."""
    try:
        import copernicusmarine

        cat = copernicusmarine.describe(product_id=product_id)
        return sorted({d.dataset_id for p in cat.products for d in p.datasets})
    except Exception:
        return []


def probe_points(source: str, points: list, t0, creds: Creds = None,
                 custom_ids: str = "", progress: ProgressCB = None) -> list:
    """Find the grid cell for each point. `points`: dicts with name/lat/lon.

    Returns one dict per point: name, req_lat, req_lon, the probe_cell fields and,
    for Copernicus sources, coverage_start / coverage_end of the dataset(s).
    """
    spec = SOURCES[source]
    names = spec["names"]
    rows: list = []

    if spec["kind"] == "hycom":
        year = int(to_ts(t0).year) if source == "reanalysis" else None
        ds = open_ds(dataset_url(source, year), year=year)
        try:
            for p in points:
                rows.append({"name": p["name"], "req_lat": p["lat"], "req_lon": p["lon"],
                             **probe_cell(ds, p["lat"], p["lon"], names=names)})
        finally:
            ds.close()
        return rows

    ids = source_datasets(source, custom_ids)
    for k, p in enumerate(points):
        if progress:
            progress(k / max(len(points), 1), f"{p['name']}")
        first: Optional[dict] = None
        lo_t: Optional[pd.Timestamp] = None
        hi_t: Optional[pd.Timestamp] = None
        failures: list = []
        for did in ids:
            try:
                ds = open_cmems(did, p["lat"], p["lon"], creds)
            except CmemsAuthError:
                raise
            except Exception as exc:
                failures.append(f"{did}: {exc}")
                continue
            try:
                times = pd.DatetimeIndex(ds[names["time"]].values)
                if len(times):
                    lo_t = to_ts(times[0]) if lo_t is None else min(lo_t, to_ts(times[0]))
                    hi_t = to_ts(times[-1]) if hi_t is None else max(hi_t, to_ts(times[-1]))
                if first is None:
                    first = probe_cell(ds, p["lat"], p["lon"], names=names)
            finally:
                ds.close()
        if first is None:
            hint = list_cmems_datasets(spec["product"])
            msg = "; ".join(failures) or "no dataset could be opened"
            if hint:
                msg += f". Datasets in {spec['product']}: {', '.join(hint)}"
            raise RuntimeError(msg)
        rows.append({
            "name": p["name"], "req_lat": p["lat"], "req_lon": p["lon"], **first,
            "coverage_start": str(lo_t.date()) if lo_t is not None else None,
            "coverage_end": str(hi_t.date()) if hi_t is not None else None,
        })
    return rows


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def _column_to_frame(da: xr.DataArray, depths, include_bottom: bool,
                     nearest: bool = False) -> pd.DataFrame:
    """da dims: (time, depth) at a single cell -> wide DataFrame.

    nearest=True maps each requested depth to the closest model level
    (Copernicus levels are not round numbers); otherwise levels must match exactly.
    """
    full = da.load()
    all_depths = full["depth"].values
    if depths:
        if nearest:
            idx = sorted({int(np.abs(all_depths - float(x)).argmin()) for x in depths})
            sel = full.isel(depth=idx)
        else:
            keep = [d for d in all_depths if float(d) in {float(x) for x in depths}]
            sel = full.sel(depth=keep)
    else:
        sel = full
    df = cast(pd.DataFrame, sel.to_pandas())
    df.columns = [f"Te{round(float(d), 3):g}m" for d in df.columns]
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
    creds: Creds = None,
    custom_ids: str = "",
) -> pd.DataFrame:
    """Extract a temperature time series (whole column or chosen levels)."""
    t0, t1 = to_ts(t0), to_ts(t1)
    spec = SOURCES[source]
    names = spec["names"]
    la, lo = cell["la"], cell["lo"]
    frames = []

    def say(frac, msg):
        if progress:
            progress(min(max(frac, 0.0), 1.0), msg)

    if spec["kind"] == "cmems":
        ids = source_datasets(source, custom_ids)
        limit = t1 + pd.Timedelta(hours=23)
        if spec.get("forecast"):  # never return forecast steps
            limit = min(limit, pd.Timestamp.now(tz="UTC").tz_localize(None))
        for k, did in enumerate(ids):
            say(k / len(ids), f"opening {did}")
            ds = open_cmems(did, cell["lat_label"], cell["lon_label"], creds)
            times = pd.DatetimeIndex(ds[names["time"]].values)
            if len(times) == 0:
                ds.close()
                continue
            s0, e0 = max(t0, to_ts(times[0])), min(limit, to_ts(times[-1]))
            if s0 > e0:  # this dataset does not cover the requested period
                ds.close()
                continue
            point = ds[names["var"]].sel(
                {names["lat"]: cell["lat_label"], names["lon"]: cell["lon_label"]},
                method="nearest",
            )
            s = s0
            span = max(cast(pd.Timedelta, e0 - s0).total_seconds(), 86400.0)
            while s <= e0:
                e = min(s + pd.Timedelta(days=365), e0)
                done = cast(pd.Timedelta, s - s0).total_seconds()
                say((k + done / span) / len(ids), f"{did}: {s.date()} -> {e.date()}")
                da = point.sel({names["time"]: slice(s, e)})
                if da.sizes[names["time"]]:
                    frames.append(_column_to_frame(da, depths, include_bottom, nearest=True))
                s = e + pd.Timedelta(seconds=1)
            ds.close()
    elif source == "analysis":
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
            ds = open_ds(dataset_url("reanalysis", y), year=y)
            da = ds["water_temp"].isel(lat=la, lon=lo).sel(
                time=slice(t0, t1 + pd.Timedelta(hours=23))
            )
            if da.sizes["time"]:
                frames.append(_column_to_frame(da, depths, include_bottom))
            ds.close()

    if not frames:
        raise RuntimeError("No data returned for that period.")
    df = cast(pd.DataFrame, pd.concat(frames))
    df = df[~df.index.duplicated()].sort_index()
    df.index.name = "datetime_utc"
    te = [c for c in df.columns if c.startswith("Te")]
    df = cast(pd.DataFrame, df.loc[df.loc[:, te].notna().any(axis=1)])
    if hourly and spec["kind"] == "hycom":  # daily Copernicus data is never interpolated
        df = cast(pd.DataFrame, df.resample("1h").interpolate(method="time", limit=8))
    say(1.0, "done")
    return df.round(3)


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #
def build_metadata(name, lat, lon, cell, df, cfg) -> dict:
    te = [c for c in df.columns if c.startswith("Te")]
    spec = SOURCES[cfg["source"]]
    if spec["kind"] == "hycom":
        cadence = "1 h (interpolated)" if cfg["hourly"] else f"{spec['step']} (native)"
    else:
        cadence = f"{spec['step']} (native)"
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
        "cadence": cadence,
        "variable": spec["names"]["var"],
        "datasets": source_datasets(cfg["source"], cfg.get("custom_ids", ""))
        if spec["kind"] == "cmems" else [dataset_url(cfg["source"])],
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
        variables = sorted({m.get("variable", "water_temp") for m in metas.values()})
        credits = []
        for m in metas.values():
            c = SOURCES[m["source"]].get("credit")
            if c and c not in credits:
                credits.append(c)
        notes = [
            f"Times are UTC. Temperatures are in degrees C (model variable: {', '.join(variables)}).",
            "Columns named Te<depth>m (for example Te20m) are the temperature at that depth in metres.",
            "TeBottom is the temperature at the deepest level with data; bottom_depth_m is that level's depth.",
            "A blank cell means no data (for example, a level below the seabed).",
            "Values are for the HYCOM grid cell shown above, which can sit a few km from the requested point.",
        ] + [f"Source: {c}" for c in credits]
        if "thetao" in variables:
            notes.append("Copernicus thetao is sea water potential temperature; the data are daily means.")
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
