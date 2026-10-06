"""
Ocean Temperature Extractor - a step-by-step Streamlit app (data extraction only).
Sources: HYCOM (no login) and Copernicus Marine (login entered at run time).

Run locally:   streamlit run app.py
Cloud:         see README.md (Streamlit Community Cloud, Cloud Run, any VM)
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from typing import cast

import altair as alt
import pandas as pd
import streamlit as st

import hycom_core as hc

st.set_page_config(page_title="Ocean Temperature Extractor", layout="wide")

STEPS = [
    "1 · Locations",
    "2 · Dataset & dates",
    "3 · Depths & format",
    "4 · Check grid cells",
    "5 · Extract & download",
]
MAX_POINTS = 20
DEFAULT_RANGE_DAYS = 7
LONG_RANGE_WARNING_DAYS = 7
BLUE, ORANGE = "#1f77b4", "#ff7f0e"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def init_state():
    ss = st.session_state
    if "init" in ss:
        return
    ss.init = True
    ss.step = 0
    ss.locs_base = pd.DataFrame(
        [
            {"name": "Thai Binh", "lat": 20.2770, "lon": 106.7117},
            {"name": "Ham Rong", "lat": 20.1255, "lon": 107.2126},
        ]
    )
    ss.locs_edit = ss.locs_base.copy()
    ss.editor_v = 0
    ss.cfg = {
        "source": "espcd_v02",
        "_last_source": "espcd_v02",
        "t0": date.today() - timedelta(days=DEFAULT_RANGE_DAYS - 1),
        "t1": date.today(),
        "depth_mode": "whole",
        "levels": [0, 4, 20, 30, 50],
        "levels_cmems": [0.0, 5.0, 10.0, 20.0, 30.0, 50.0],
        "custom_ids": "",
        "include_bottom": True,
        "hourly": False,
    }
    ss["_cm_user"] = ""   # Copernicus login: kept in this session's memory only
    ss["_cm_pass"] = ""
    ss.probe = None
    ss.results = None


def get_creds():
    """Copernicus login typed into the app (session memory only), or None."""
    ss = st.session_state
    user, pw = ss.get("_cm_user", ""), ss.get("_cm_pass", "")
    return {"username": user, "password": pw} if user and pw else None


def kind_of(source: str) -> str:
    return hc.SOURCES[source]["kind"]


def provider_of(source: str) -> str:
    return "HYCOM" if kind_of(source) == "hycom" else "Copernicus Marine"


def parse_depths(text: str):
    """'0, 5 10;20' -> ([0.0, 5.0, 10.0, 20.0], [bad tokens])."""
    vals, bad = [], []
    for tok in re.split(r"[,\s;]+", text.strip()):
        if not tok:
            continue
        try:
            v = float(tok)
            (vals if v >= 0 else bad).append(v if v >= 0 else tok)
        except ValueError:
            bad.append(tok)
    return sorted(set(vals)), bad


def current_depths(cfg):
    if cfg["depth_mode"] == "whole":
        return None
    key = "levels" if kind_of(cfg["source"]) == "hycom" else "levels_cmems"
    return [float(x) for x in cfg[key]]


def clean_locs(df: pd.DataFrame):
    """Return (valid_df, list_of_error_strings)."""
    errors = []
    d = df.dropna(how="all").copy()
    for col in ("name", "lat", "lon"):
        if col not in d:
            d[col] = None
    incomplete = d["name"].isna() | d["lat"].isna() | d["lon"].isna()
    if incomplete.any():
        errors.append(f"{int(incomplete.sum())} row(s) are incomplete (need name, lat and lon).")
        d = cast(pd.DataFrame, d.loc[~incomplete])
    d["name"] = d["name"].map(lambda x: str(x).strip())
    d["lat"] = pd.to_numeric(d["lat"], errors="coerce")
    d["lon"] = pd.to_numeric(d["lon"], errors="coerce")
    d = cast(pd.DataFrame, d.loc[d["lat"].notna() & d["lon"].notna()])
    if (d["name"] == "").any():
        errors.append("Every location needs a name.")
    if d["name"].duplicated().any():
        errors.append("Location names must be unique.")
    if ((d["lat"] < -80) | (d["lat"] > 80)).any():
        errors.append("Latitude must be between -80 and 80.")
    if ((d["lon"] < -180) | (d["lon"] > 360)).any():
        errors.append("Longitude must be between -180 and 360.")
    if len(d) == 0:
        errors.append("Add at least one location.")
    if len(d) > MAX_POINTS:
        errors.append(f"Maximum {MAX_POINTS} locations per run.")
    return cast(pd.DataFrame, d.reset_index(drop=True)), errors


def config_sig(locs: pd.DataFrame) -> str:
    cfg = {k: v for k, v in st.session_state.cfg.items() if not k.startswith("_")}
    return json.dumps(
        {"locs": locs.round(5).to_dict("records"), "cfg": cfg}, sort_keys=True, default=str
    )


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
def step_locations() -> bool:
    ss = st.session_state
    st.subheader("Where do you want data?")
    st.caption(
        "Edit the table directly, or add points from UTM coordinates or a CSV. "
        "Longitude can be -180..180 or 0..360."
    )

    edited = st.data_editor(
        ss.locs_base,
        key=f"locs_editor_{ss.editor_v}",
        num_rows="dynamic",
        column_config={
            "name": st.column_config.TextColumn("Name", required=True),
            "lat": st.column_config.NumberColumn("Latitude (°N)", format="%.4f", required=True),
            "lon": st.column_config.NumberColumn("Longitude (°E)", format="%.4f", required=True),
        },
    )
    ss.locs_edit = edited

    c1, c2 = st.columns(2)
    with c1, st.expander("Add a point from UTM coordinates"):
        name = st.text_input("Name", "UTM point", key="utm_name")
        z1, z2 = st.columns(2)
        zone = z1.number_input("UTM zone", 1, 60, 48, key="utm_zone")
        hemi = z2.radio("Hemisphere", ["North", "South"], horizontal=True, key="utm_hemi")
        e1, n1 = st.columns(2)
        easting = e1.number_input("Easting (m)", value=500000.0, step=1.0, format="%.0f", key="utm_e")
        northing = n1.number_input("Northing (m)", value=2000000.0, step=1.0, format="%.0f", key="utm_n")
        if st.button("Add to table", key="utm_add"):
            lat, lon = hc.utm_to_latlon(easting, northing, zone, hemi == "North")
            new = pd.DataFrame([{"name": name, "lat": round(lat, 4), "lon": round(lon, 4)}])
            ss.locs_base = pd.concat([ss.locs_edit, new], ignore_index=True)
            ss.editor_v += 1
            st.rerun()
    with c2, st.expander("Upload a CSV of points"):
        st.caption("Columns: name (optional), lat / latitude, lon / longitude.")
        up = st.file_uploader("CSV file", type="csv", key="csv_up")
        if up is not None and st.button("Add file's points", key="csv_add"):
            try:
                raw = pd.read_csv(up)
                raw.columns = [c.strip().lower() for c in raw.columns]
                ren = {"latitude": "lat", "longitude": "lon", "lng": "lon", "long": "lon"}
                raw = raw.rename(columns=ren)
                if "name" not in raw:
                    raw["name"] = [f"Point {i + 1}" for i in range(len(raw))]
                new = raw[["name", "lat", "lon"]]
                ss.locs_base = pd.concat([ss.locs_edit, new], ignore_index=True)
                ss.editor_v += 1
                st.rerun()
            except Exception as exc:
                st.error(f"Could not read that file: {exc}")

    locs, errors = clean_locs(ss.locs_edit)
    for e in errors:
        st.error(e)
    if not errors:
        st.map(locs, latitude="lat", longitude="lon", size=1500)
        st.success(f"{len(locs)} location(s) ready.")
    return not errors


def step_source() -> bool:
    cfg = st.session_state.cfg
    st.subheader("Which dataset and period?")
    keys = list(hc.SOURCES)
    if cfg.get("source") not in keys:
        cfg["source"] = "espcd_v02" if "espcd_v02" in keys else keys[0]
    if cfg.get("_last_source") not in keys:
        cfg["_last_source"] = cfg["source"]
    cfg["source"] = st.radio(
        "Dataset",
        keys,
        index=keys.index(cfg["source"]),
        format_func=lambda k: hc.SOURCES[k]["label"],
    )
    meta = hc.SOURCES[cfg["source"]]
    kind = meta["kind"]
    lo = meta["start"].date()
    hi = meta["end"].date() if meta["end"] is not None else date.today()
    if kind == "hycom" and meta["end"] is None:
        ss = st.session_state
        try:
            coverage_key = f"_hycom_coverage_{cfg['source']}"
            coverage = ss.get(coverage_key)
            if coverage is None:
                coverage = hc.hycom_coverage(cfg["source"])
                ss[coverage_key] = coverage
        except Exception as exc:
            st.error(f"Could not read the live HYCOM dataset coverage: {exc}")
            return False
        coverage_start, coverage_end = (value.date() for value in coverage)
        lo = max(lo, coverage_start)
        hi = min(hi, coverage_end)
        if lo > hi:
            st.error(f"The HYCOM analysis feed has no data within the advertised range {lo} to {hi}.")
            return False

    if cfg["source"] != cfg["_last_source"]:  # sensible defaults when switching
        cfg["_last_source"] = cfg["source"]
        cfg["t1"] = hi
        cfg["t0"] = max(lo, hi - timedelta(days=DEFAULT_RANGE_DAYS - 1))
    elif cfg["t0"] > hi:
        cfg["t1"] = hi
        cfg["t0"] = max(lo, hi - timedelta(days=DEFAULT_RANGE_DAYS - 1))
    elif cfg["t1"] > hi:
        period = cfg["t1"] - cfg["t0"]
        cfg["t1"] = hi
        cfg["t0"] = max(lo, hi - period)

    cfg["t0"] = min(max(cfg["t0"], lo), hi)
    cfg["t1"] = min(max(cfg["t1"], lo), hi)

    if kind == "hycom":
        st.caption(
            f"Available: {lo} to {hi}. "
            "3-hourly, times in UTC. "
            + (
                "Coverage is read from the live HYCOM dataset; forecast steps are excluded."
                if kind == "hycom" and meta["end"] is None
                else "Static dataset - ends 2015-12-30."
            )
        )
    else:
        st.caption(
            "Daily means, times in UTC. The dates offered are nominal; the real coverage is "
            "read from Copernicus Marine in step 4. "
            + ("Forecast days are excluded." if meta.get("forecast") else "")
        )
    a, b = st.columns(2)
    cfg["t0"] = a.date_input("From", cfg["t0"], min_value=lo, max_value=hi)
    cfg["t1"] = b.date_input("To", cfg["t1"], min_value=lo, max_value=hi)

    if cfg["t0"] > cfg["t1"]:
        st.error("'From' must be on or before 'To'.")
        return False
    days = (cfg["t1"] - cfg["t0"]).days + 1
    per_day = 8 if kind == "hycom" else 1
    st.info(f"{days} days ≈ {days * per_day:,} time steps per location.")
    if days > LONG_RANGE_WARNING_DAYS:
        st.warning(
            f"Ranges longer than {LONG_RANGE_WARNING_DAYS} days can take substantially longer. "
            "Extract one location at a time; for HYCOM, the current ESPC-D-V02 source is split "
            "into daily requests."
        )

    if kind == "cmems":
        ss = st.session_state
        st.markdown("**Copernicus Marine login**")
        env_ok = bool(os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME")
                      and os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD"))
        if env_ok:
            st.success("A login is set on the server (environment variables). The boxes below are optional.")
        u = st.text_input("Username (email)", value=ss.get("_cm_user", ""), key="cm_user_in")
        p = st.text_input("Password", value=ss.get("_cm_pass", ""), type="password", key="cm_pass_in")
        ss["_cm_user"], ss["_cm_pass"] = (u or "").strip(), (p or "")
        st.caption(
            "Used only in this browser session. It is never saved to disk or included in the "
            "downloads, and 'Start over' clears it. Create an account at marine.copernicus.eu "
            "if you do not have one."
        )
        with st.expander("Advanced: Copernicus dataset IDs"):
            default_ids = ", ".join(meta["datasets"])
            val = st.text_input(
                "Dataset IDs, comma-separated (several are joined in time order)",
                value=cfg.get("custom_ids") or default_ids,
                key=f"ids_{cfg['source']}",
            )
            val = (val or "").strip()
            cfg["custom_ids"] = "" if val == default_ids else val
            st.caption("Leave as is unless step 4 says a dataset was not found; it then lists the IDs "
                       f"that exist in {meta['product']}.")
        if not (hc.have_login(get_creds())):
            st.error("Enter your Copernicus Marine username and password to continue.")
            return False
    return True


def step_depths() -> bool:
    cfg = st.session_state.cfg
    st.subheader("Which depths, and what format?")
    mode = st.radio(
        "Depth levels",
        ["whole", "levels"],
        index=0 if cfg["depth_mode"] == "whole" else 1,
        format_func=lambda m: "Whole water column (every level down to the seabed)"
        if m == "whole"
        else "Choose specific levels",
    )
    cfg["depth_mode"] = mode
    kind = kind_of(cfg["source"])
    if mode == "levels":
        if kind == "hycom":
            cfg["levels"] = st.multiselect(
                "Levels (m)", hc.HYCOM_LEVELS, default=[l for l in cfg["levels"] if l in hc.HYCOM_LEVELS]
            )
            st.caption("HYCOM has fixed levels (no 3 m, for example). Levels below the seabed are dropped.")
        else:
            txt = st.text_input(
                "Depths in metres, comma-separated",
                value=", ".join(f"{v:g}" for v in cfg["levels_cmems"]),
            )
            vals, bad = parse_depths(txt or "")
            if bad:
                st.error("Not valid depths: " + ", ".join(map(str, bad)))
            else:
                cfg["levels_cmems"] = vals
            st.caption("Each depth is matched to the nearest model level; the column names show the "
                       "level actually used. Levels below the seabed are dropped.")
    cfg["include_bottom"] = st.checkbox(
        "Also add the bottom temperature (TeBottom) and the bottom depth", value=cfg["include_bottom"]
    )
    if kind == "hycom":
        cfg["hourly"] = (
            st.radio(
                "Time step",
                ["3 h (native)", "1 h (linear interpolation)"],
                index=1 if cfg["hourly"] else 0,
                horizontal=True,
            ).startswith("1 h")
        )
    else:
        cfg["hourly"] = False
        st.caption("Copernicus data are daily means; no time interpolation is applied.")
    if mode == "levels":
        chosen = cfg["levels"] if kind == "hycom" else cfg["levels_cmems"]
        if not chosen:
            st.error("Pick at least one depth.")
            return False
    return True


def step_check(locs: pd.DataFrame) -> bool:
    ss = st.session_state
    cfg = ss.cfg
    sig = config_sig(locs)
    provider = provider_of(cfg["source"])
    st.subheader("Check where each point lands on the model grid")
    st.caption(
        "The model grid is ~8-9 km, so a point snaps to the nearest cell that is ocean. "
        "Coastal points may move; this shows by how much."
    )
    if st.button("Check locations", type="primary"):
        with st.spinner(f"Contacting {provider}..."):
            try:
                points = [
                    {"name": str(rec["name"]), "lat": float(rec["lat"]), "lon": float(rec["lon"])}
                    for rec in locs.to_dict("records")
                ]
                rows = hc.probe_points(
                    cfg["source"], points, hc.to_ts(cfg["t0"]),
                    creds=get_creds(), custom_ids=cfg.get("custom_ids", ""),
                )
                ss.probe = {"sig": sig, "rows": rows}
                ss.results = None
            except Exception as exc:
                if isinstance(exc, hc.CmemsAuthError):
                    st.error(str(exc))
                else:
                    st.error(f"Could not reach {provider}: {exc}")
                ss.probe = None

    probe = ss.probe
    if probe is None or probe["sig"] != sig:
        st.info("Press **Check locations** to continue.")
        return False

    rows = probe["rows"]
    table = pd.DataFrame(
        [
            {
                "Name": r["name"],
                "Requested lat": r["req_lat"],
                "Requested lon": r["req_lon"],
                "Grid lat": r.get("grid_lat"),
                "Grid lon": r.get("grid_lon"),
                "Offset (km)": r.get("offset_km"),
                "Water depth (m)": r.get("bottom_depth_m"),
                "Levels with data": r.get("n_levels"),
                "Dataset covers": (f"{r['coverage_start']} to {r['coverage_end']}"
                                   if r.get("coverage_start") else "-"),
                "Status": "OK" if r["ok"] else r.get("reason", "no ocean cell"),
            }
            for r in rows
        ]
    )
    if all(v == "-" for v in table["Dataset covers"]):
        table = table.drop(columns=["Dataset covers"])
    st.dataframe(table, hide_index=True)

    pts = []
    for r in rows:
        pts.append({"lat": r["req_lat"], "lon": hc.wrap_lon180(r["req_lon"]), "color": BLUE})
        if r["ok"]:
            pts.append({"lat": r["grid_lat"], "lon": r["grid_lon"], "color": ORANGE})
    st.map(pd.DataFrame(pts), latitude="lat", longitude="lon", color="color", size=1500)
    st.caption("Blue = requested point, orange = model grid cell that will be used.")

    bad = [r["name"] for r in rows if not r["ok"]]
    if bad:
        st.error("No ocean cell found for: " + ", ".join(bad) + ". Move the point offshore or remove it.")
        return False
    far = [r["name"] for r in rows if r["offset_km"] > 12]
    if far:
        st.warning("These points snap more than 12 km away: " + ", ".join(far))

    # Copernicus: compare the requested period with the real dataset coverage.
    t0, t1 = cfg["t0"], cfg["t1"]
    outside, partial = [], []
    for r in rows:
        if not r.get("coverage_start"):
            continue
        c0, c1 = date.fromisoformat(r["coverage_start"]), date.fromisoformat(r["coverage_end"])
        if t1 < c0 or t0 > c1:
            outside.append(f"{r['name']} ({c0} to {c1})")
        elif t0 < c0 or t1 > c1:
            partial.append(f"{r['name']} ({c0} to {c1})")
    if outside:
        st.error("The requested period is outside the dataset coverage for: " + "; ".join(outside)
                 + ". Change the dates in step 2.")
        return False
    if partial:
        st.warning("The requested period is only partly covered, so you will get less data: "
                   + "; ".join(partial))
    return True


def step_extract(locs: pd.DataFrame):
    ss = st.session_state
    cfg = ss.cfg
    sig = config_sig(locs)
    st.subheader("Extract and download")
    if ss.probe is None or ss.probe["sig"] != sig:
        st.warning("Settings changed - go back to step 4 and re-check the locations.")
        return

    probe_rows = ss.probe["rows"]
    location_names = [row["name"] for row in probe_rows if row["ok"]]
    if not location_names:
        st.error("There are no checked ocean locations to extract.")
        return
    selected_name = st.selectbox("Location to extract", location_names, key="extract_location")
    run_sig = json.dumps({"config": sig, "location": selected_name}, sort_keys=True)
    depths = current_depths(cfg)
    spec = hc.SOURCES[cfg["source"]]
    cadence = "hourly (interpolated)" if (cfg["hourly"] and spec["kind"] == "hycom") else f"{spec['step']} native"
    st.write(
        f"**{selected_name}** · {provider_of(cfg['source'])} · "
        f"{cfg['t0']} → {cfg['t1']} · "
        f"{'whole water column' if depths is None else str(len(depths)) + ' depths'} · {cadence}"
    )
    if st.button("Start extraction", type="primary"):
        row = next(r for r in probe_rows if r["name"] == selected_name)
        data, metas, errors = {}, {}, {}
        bar = st.progress(0.0)
        status = st.empty()
        def cb(frac, msg):
            bar.progress(min(frac, 1.0))
            status.write(f"**{selected_name}** - {msg}")

        try:
            df = hc.extract_point(
                cfg["source"], row, hc.to_ts(cfg["t0"]), hc.to_ts(cfg["t1"]),
                depths=depths, include_bottom=cfg["include_bottom"],
                hourly=cfg["hourly"], progress=cb,
                creds=get_creds(), custom_ids=cfg.get("custom_ids", ""),
            )
            data[selected_name] = df
            metas[selected_name] = hc.build_metadata(
                selected_name, row["req_lat"], row["req_lon"], row, df, cfg
            )
        except Exception as exc:
            errors[selected_name] = str(exc)
        bar.progress(1.0)
        status.empty()
        xlsx, xlsx_err = None, None
        if data:
            with st.spinner("Building Excel workbook..."):
                try:
                    xlsx = hc.make_xlsx(data, metas)
                except Exception as exc:
                    xlsx_err = str(exc)
        ss.results = {
            "sig": run_sig, "data": data, "metas": metas, "errors": errors,
            "xlsx": xlsx, "xlsx_err": xlsx_err,
        }

    res = ss.results
    if not res or res["sig"] != run_sig:
        return
    for name, msg in res["errors"].items():
        st.error(f"{name}: {msg}")
    if not res["data"]:
        return

    st.success(f"Extracted {selected_name}.")
    d1, d2 = st.columns(2)
    if res.get("xlsx"):
        d1.download_button(
            "Download Excel workbook (Summary + one sheet per site)",
            res["xlsx"],
            file_name="ocean_temperature_extract.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
    d2.download_button(
        "Download ZIP (one CSV per site + metadata.json)",
        hc.make_zip(res["data"], res["metas"]),
        file_name="ocean_temperature_extract.zip",
        mime="application/zip",
    )
    if res.get("xlsx_err"):
        st.warning(f"Excel workbook not built: {res['xlsx_err']}")
    tabs = st.tabs(list(res["data"]))
    for tab, (name, df) in zip(tabs, res["data"].items()):
        with tab:
            m = res["metas"][name]
            a, b, c, d = st.columns(4)
            a.metric("Rows", f"{m['rows']:,}")
            b.metric("Water depth (m)", f"{m['median_bottom_depth_m']:.0f}")
            c.metric("Min seabed temp (°C)", "n/a" if m.get("min_bottom_temperature_c") is None else f"{m['min_bottom_temperature_c']:.2f}")
            d.metric("Grid offset (km)", m["offset_km"])
            st.subheader("Vertical temperature profile")
            st.caption(
                "Temperature is plotted against the model depth levels available at this "
                "grid cell. Depth increases downward; missing levels are omitted. When "
                "bottom temperature is enabled in step 3, the seabed/bottom point is shown "
                "separately. It is the deepest valid model level, which may be above the "
                "actual seabed."
            )
            first_day = df.index.min().date()
            last_day = df.index.max().date()
            profile_key = f"{hc.slug(name)}_{cfg['source']}_{cfg['t0']}_{cfg['t1']}"
            profile_day = st.date_input(
                "Profile date (UTC)",
                value=last_day,
                min_value=first_day,
                max_value=last_day,
                key=f"profile_date_{profile_key}",
            )
            day_times = df.index[df.index.date == profile_day]
            time_labels = [timestamp.strftime("%H:%M") for timestamp in day_times]
            selected_time = st.selectbox(
                "Profile time (UTC)",
                time_labels,
                index=len(time_labels) - 1,
                format_func=lambda value: f"{value} UTC",
                key=f"profile_time_{profile_key}_{profile_day}",
            )
            selected_timestamp = day_times[time_labels.index(selected_time)]
            profile_depths, profile_temperatures = [], []
            for col in df.columns:
                match = re.fullmatch(r"Te(\d+(?:\.\d+)?)m", str(col))
                if match is not None:
                    profile_depths.append(float(match.group(1)))
                    profile_temperatures.append(df.at[selected_timestamp, col])
            profile = pd.DataFrame(
                {"depth_m": profile_depths, "temperature_c": profile_temperatures}
            ).dropna().sort_values("depth_m")
            seabed_profile = pd.DataFrame(columns=["depth_m", "temperature_c"])
            has_seabed = (
                "TeBottom" in df
                and "bottom_depth_m" in df
                and pd.notna(df.at[selected_timestamp, "TeBottom"])
                and pd.notna(df.at[selected_timestamp, "bottom_depth_m"])
            )
            if has_seabed:
                seabed_profile = pd.DataFrame(
                    [{
                        "depth_m": float(df.at[selected_timestamp, "bottom_depth_m"]),
                        "temperature_c": float(df.at[selected_timestamp, "TeBottom"]),
                    }]
                )
            if profile.empty and seabed_profile.empty:
                st.info("No vertical or seabed temperature data are available at this time.")
            else:
                x_encoding = alt.X("temperature_c:Q", title="Temperature (°C)")
                y_encoding = alt.Y(
                    "depth_m:Q",
                    title="Depth below sea surface (m)",
                    scale=alt.Scale(reverse=True),
                )
                tooltip = [
                    alt.Tooltip("depth_m:Q", title="Depth (m)", format=".1f"),
                    alt.Tooltip(
                        "temperature_c:Q",
                        title="Temperature (°C)",
                        format=".2f",
                    ),
                ]
                if profile.empty:
                    chart = (
                        alt.Chart(seabed_profile)
                        .mark_point(shape="diamond", filled=True, size=110, color=ORANGE)
                        .encode(x=x_encoding, y=y_encoding, tooltip=tooltip)
                        .properties(height=360)
                    )
                else:
                    chart = (
                        alt.Chart(profile)
                        .mark_line(point=True, color=BLUE)
                        .encode(
                            x=x_encoding,
                            y=y_encoding,
                            tooltip=tooltip,
                        )
                        .properties(height=360)
                    )
                    if not seabed_profile.empty:
                        seabed_point = (
                            alt.Chart(seabed_profile)
                            .mark_point(shape="diamond", filled=True, size=110, color=ORANGE)
                            .encode(x=x_encoding, y=y_encoding, tooltip=tooltip)
                        )
                        chart = chart + seabed_point
                st.altair_chart(chart, use_container_width=True)
            if not seabed_profile.empty:
                bottom_cols = st.columns(2)
                bottom_cols[0].metric(
                    "Seabed / bottom temperature (°C)",
                    f"{seabed_profile.iloc[0]['temperature_c']:.2f}",
                )
                bottom_cols[1].metric(
                    "Seabed / bottom depth (m)",
                    f"{seabed_profile.iloc[0]['depth_m']:.1f}",
                )
            te = [col for col in df.columns if col.startswith("Te")]
            pick = st.multiselect("Quick look", te, default=te[:1] + te[-1:], key=f"pick_{name}")
            if pick:
                view = df[pick]
                if len(view) > 4000:
                    view = view.iloc[:: len(view) // 4000 + 1]
                st.line_chart(view)
            st.dataframe(df.head(200))
            st.download_button(
                f"Download {name}.csv",
                df.to_csv().encode(),
                file_name=f"{hc.slug(name)}.csv",
                mime="text/csv",
                key=f"dl_{name}",
            )


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def main():
    init_state()
    ss = st.session_state

    with st.sidebar:
        st.title("Ocean Temperature Extractor")
        st.caption("HYCOM and Copernicus Marine ocean temperature at any coordinate.")
        for i, label in enumerate(STEPS):
            st.markdown(("**→ " + label + "**") if i == ss.step else ("✓ " + label if i < ss.step else label))
        st.divider()
        if st.button("Start over"):
            for k in list(ss.keys()):
                del ss[k]
            st.rerun()

    locs, _ = clean_locs(ss.locs_edit)

    if ss.step == 0:
        ready = step_locations()
    elif ss.step == 1:
        ready = step_source()
    elif ss.step == 2:
        ready = step_depths()
    elif ss.step == 3:
        ready = step_check(locs)
    else:
        step_extract(locs)
        ready = False

    st.divider()
    back, nxt, _ = st.columns([1, 1, 6])
    if ss.step > 0 and back.button("← Back", key="nav_back"):
        ss.step -= 1
        st.rerun()
    if ss.step < len(STEPS) - 1 and nxt.button("Next →", type="primary", disabled=not ready, key="nav_next"):
        ss.step += 1
        st.rerun()


main()
