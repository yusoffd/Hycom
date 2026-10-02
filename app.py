"""
HYCOM Data Extractor - a step-by-step Streamlit app (data extraction only).

Run locally:   streamlit run app.py
Cloud:         see README.md (Streamlit Community Cloud, Cloud Run, any VM)
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import hycom_core as hc

st.set_page_config(page_title="HYCOM Data Extractor", layout="wide")

STEPS = [
    "1 · Locations",
    "2 · Dataset & dates",
    "3 · Depths & format",
    "4 · Check grid cells",
    "5 · Extract & download",
]
MAX_POINTS = 20
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
        "source": "analysis",
        "_last_source": "analysis",
        "t0": date.today() - timedelta(days=365),
        "t1": date.today(),
        "depth_mode": "whole",
        "levels": [0, 4, 20, 30, 50],
        "include_bottom": True,
        "hourly": False,
    }
    ss.probe = None
    ss.results = None


def clean_locs(df: pd.DataFrame):
    """Return (valid_df, list_of_error_strings)."""
    errors = []
    d = df.dropna(how="all").copy()
    for col in ("name", "lat", "lon"):
        if col not in d:
            d[col] = None
    incomplete = d[["name", "lat", "lon"]].isna().any(axis=1)
    if incomplete.any():
        errors.append(f"{int(incomplete.sum())} row(s) are incomplete (need name, lat and lon).")
        d = d[~incomplete]
    d["name"] = d["name"].astype(str).str.strip()
    d["lat"] = pd.to_numeric(d["lat"], errors="coerce")
    d["lon"] = pd.to_numeric(d["lon"], errors="coerce")
    d = d.dropna(subset=["lat", "lon"])
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
    return d.reset_index(drop=True), errors


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
    cfg["source"] = st.radio(
        "Dataset",
        keys,
        index=keys.index(cfg["source"]),
        format_func=lambda k: hc.SOURCES[k]["label"],
    )
    meta = hc.SOURCES[cfg["source"]]
    lo = meta["start"].date()
    hi = meta["end"].date() if meta["end"] is not None else date.today()

    if cfg["source"] != cfg["_last_source"]:  # sensible defaults when switching
        cfg["_last_source"] = cfg["source"]
        cfg["t1"] = hi
        cfg["t0"] = max(lo, hi - timedelta(days=365))

    cfg["t0"] = min(max(cfg["t0"], lo), hi)
    cfg["t1"] = min(max(cfg["t1"], lo), hi)

    st.caption(
        f"Available: {lo} to {'today' if meta['end'] is None else hi}. "
        "3-hourly, times in UTC. "
        + (
            "Includes the most recent days; forecast steps are excluded."
            if cfg["source"] == "analysis"
            else "Static dataset - ends 2015-12-30."
        )
    )
    a, b = st.columns(2)
    cfg["t0"] = a.date_input("From", cfg["t0"], min_value=lo, max_value=hi)
    cfg["t1"] = b.date_input("To", cfg["t1"], min_value=lo, max_value=hi)

    if cfg["t0"] > cfg["t1"]:
        st.error("'From' must be on or before 'To'.")
        return False
    days = (cfg["t1"] - cfg["t0"]).days + 1
    st.info(f"{days} days ≈ {days * 8:,} time steps per location.")
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
    if mode == "levels":
        cfg["levels"] = st.multiselect(
            "Levels (m)", hc.HYCOM_LEVELS, default=[l for l in cfg["levels"] if l in hc.HYCOM_LEVELS]
        )
        st.caption("HYCOM has fixed levels (no 3 m, for example). Levels below the seabed are dropped.")
    cfg["include_bottom"] = st.checkbox(
        "Also add the bottom temperature (TeBottom) and the bottom depth", value=cfg["include_bottom"]
    )
    cfg["hourly"] = (
        st.radio(
            "Time step",
            ["3 h (native)", "1 h (linear interpolation)"],
            index=1 if cfg["hourly"] else 0,
            horizontal=True,
        ).startswith("1 h")
    )
    if mode == "levels" and not cfg["levels"]:
        st.error("Pick at least one level.")
        return False
    return True


def step_check(locs: pd.DataFrame) -> bool:
    ss = st.session_state
    cfg = ss.cfg
    sig = config_sig(locs)
    st.subheader("Check where each point lands on the HYCOM grid")
    st.caption(
        "HYCOM is ~9 km resolution, so a point snaps to the nearest cell that is ocean. "
        "Coastal points may move; this shows by how much."
    )
    if st.button("Check locations", type="primary"):
        with st.spinner("Contacting HYCOM server..."):
            try:
                year = pd.Timestamp(cfg["t0"]).year if cfg["source"] == "reanalysis" else None
                ds = hc.open_ds(hc.dataset_url(cfg["source"], year))
                rows = []
                for r in locs.itertuples():
                    rows.append({"name": r.name, "req_lat": r.lat, "req_lon": r.lon,
                                 **hc.probe_cell(ds, r.lat, r.lon)})
                ds.close()
                ss.probe = {"sig": sig, "rows": rows}
                ss.results = None
            except Exception as exc:
                st.error(f"Could not reach HYCOM: {exc}")
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
                "Status": "OK" if r["ok"] else r.get("reason", "no ocean cell"),
            }
            for r in rows
        ]
    )
    st.dataframe(table, hide_index=True)

    pts = []
    for r in rows:
        pts.append({"lat": r["req_lat"], "lon": hc.wrap_lon180(r["req_lon"]), "color": BLUE})
        if r["ok"]:
            pts.append({"lat": r["grid_lat"], "lon": r["grid_lon"], "color": ORANGE})
    st.map(pd.DataFrame(pts), latitude="lat", longitude="lon", color="color", size=1500)
    st.caption("Blue = requested point, orange = HYCOM grid cell that will be used.")

    bad = [r["name"] for r in rows if not r["ok"]]
    if bad:
        st.error("No ocean cell found for: " + ", ".join(bad) + ". Move the point offshore or remove it.")
        return False
    far = [r["name"] for r in rows if r["offset_km"] > 12]
    if far:
        st.warning("These points snap more than 12 km away: " + ", ".join(far))
    return True


def step_extract(locs: pd.DataFrame):
    ss = st.session_state
    cfg = ss.cfg
    sig = config_sig(locs)
    st.subheader("Extract and download")
    if ss.probe is None or ss.probe["sig"] != sig:
        st.warning("Settings changed - go back to step 4 and re-check the locations.")
        return

    depths = None if cfg["depth_mode"] == "whole" else [float(x) for x in cfg["levels"]]
    st.write(
        f"**{len(ss.probe['rows'])} location(s)** · {cfg['t0']} → {cfg['t1']} · "
        f"{'whole water column' if depths is None else str(len(depths)) + ' levels'} · "
        f"{'hourly' if cfg['hourly'] else '3-hourly'}"
    )
    if st.button("Start extraction", type="primary"):
        rows = ss.probe["rows"]
        data, metas, errors = {}, {}, {}
        bar = st.progress(0.0)
        status = st.empty()
        n = len(rows)
        for i, r in enumerate(rows):
            def cb(frac, msg, i=i, name=r["name"]):
                bar.progress(min((i + frac) / n, 1.0))
                status.write(f"**{name}** ({i + 1}/{n}) - {msg}")

            try:
                df = hc.extract_point(
                    cfg["source"], r, pd.Timestamp(cfg["t0"]), pd.Timestamp(cfg["t1"]),
                    depths=depths, include_bottom=cfg["include_bottom"],
                    hourly=cfg["hourly"], progress=cb,
                )
                data[r["name"]] = df
                metas[r["name"]] = hc.build_metadata(r["name"], r["req_lat"], r["req_lon"], r, df, cfg)
            except Exception as exc:
                errors[r["name"]] = str(exc)
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
            "sig": sig, "data": data, "metas": metas, "errors": errors,
            "xlsx": xlsx, "xlsx_err": xlsx_err,
        }

    res = ss.results
    if not res or res["sig"] != sig:
        return
    for name, msg in res["errors"].items():
        st.error(f"{name}: {msg}")
    if not res["data"]:
        return

    st.success(f"Extracted {len(res['data'])} location(s).")
    d1, d2 = st.columns(2)
    if res.get("xlsx"):
        d1.download_button(
            "Download Excel workbook (Summary + one sheet per site)",
            res["xlsx"],
            file_name="hycom_extract.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
    d2.download_button(
        "Download ZIP (one CSV per site + metadata.json)",
        hc.make_zip(res["data"], res["metas"]),
        file_name="hycom_extract.zip",
        mime="application/zip",
    )
    if res.get("xlsx_err"):
        st.warning(f"Excel workbook not built: {res['xlsx_err']}")
    tabs = st.tabs(list(res["data"]))
    for tab, (name, df) in zip(tabs, res["data"].items()):
        with tab:
            m = res["metas"][name]
            a, b, c = st.columns(3)
            a.metric("Rows", f"{m['rows']:,}")
            b.metric("Water depth (m)", f"{m['median_bottom_depth_m']:.0f}")
            c.metric("Grid offset (km)", m["offset_km"])
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
        st.title("HYCOM Data Extractor")
        st.caption("Ocean temperature from HYCOM GOFS 3.1 at any coordinate.")
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
