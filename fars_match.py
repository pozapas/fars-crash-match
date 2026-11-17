"""
fars_match.py - find the FARS case for each crash in a state crash file.

FARS does not store the state crash ID, so a state crash and its FARS case can only be
found by what both files describe: the state, the date, the place, the time, the county
and the people killed. This module does that in two steps (see README.md):

  Step 1  location   same state and date, nearest FARS case by distance
  Step 2  attributes same state and date, then county, time and victim age and sex

Every match is then checked against the fields that were not used to make it.

Use it three ways:
  - double-click RUN_ME.bat (Windows) for a small window;
  - command line:  python fars_match.py --crashes crashes.xlsx --fars FARS_folder --out matched.xlsx
  - from Python:   import fars_match as fm; out = fm.run("crashes.xlsx", "FARS_folder", "matched.xlsx")
"""
import argparse
import glob
import os
import re
import sys
import zipfile

import numpy as np
import pandas as pd

__version__ = "1.0.0"

# ---------------------------------------------------------------- settings
NEAR_M = 150        # Step 1: accept the nearest FARS case closer than this (metres)
FAR_M = 1000        # Step 1: or closer than this, if it is the only FARS case that close
TIME_MIN = 60       # Step 2 and checks: times agree if they differ by this many minutes or less
AGE_TOL = 1         # Step 2 and checks: ages agree if they differ by this many years or less

STATES = {  # FIPS code: (name, postal code)
    1: ("Alabama", "AL"), 2: ("Alaska", "AK"), 4: ("Arizona", "AZ"), 5: ("Arkansas", "AR"),
    6: ("California", "CA"), 8: ("Colorado", "CO"), 9: ("Connecticut", "CT"), 10: ("Delaware", "DE"),
    11: ("District of Columbia", "DC"), 12: ("Florida", "FL"), 13: ("Georgia", "GA"), 15: ("Hawaii", "HI"),
    16: ("Idaho", "ID"), 17: ("Illinois", "IL"), 18: ("Indiana", "IN"), 19: ("Iowa", "IA"),
    20: ("Kansas", "KS"), 21: ("Kentucky", "KY"), 22: ("Louisiana", "LA"), 23: ("Maine", "ME"),
    24: ("Maryland", "MD"), 25: ("Massachusetts", "MA"), 26: ("Michigan", "MI"), 27: ("Minnesota", "MN"),
    28: ("Mississippi", "MS"), 29: ("Missouri", "MO"), 30: ("Montana", "MT"), 31: ("Nebraska", "NE"),
    32: ("Nevada", "NV"), 33: ("New Hampshire", "NH"), 34: ("New Jersey", "NJ"), 35: ("New Mexico", "NM"),
    36: ("New York", "NY"), 37: ("North Carolina", "NC"), 38: ("North Dakota", "ND"), 39: ("Ohio", "OH"),
    40: ("Oklahoma", "OK"), 41: ("Oregon", "OR"), 42: ("Pennsylvania", "PA"), 44: ("Rhode Island", "RI"),
    45: ("South Carolina", "SC"), 46: ("South Dakota", "SD"), 47: ("Tennessee", "TN"), 48: ("Texas", "TX"),
    49: ("Utah", "UT"), 50: ("Vermont", "VT"), 51: ("Virginia", "VA"), 53: ("Washington", "WA"),
    54: ("West Virginia", "WV"), 55: ("Wisconsin", "WI"), 56: ("Wyoming", "WY"),
}
_STATE_LOOKUP = {}
for _code, (_name, _abbr) in STATES.items():
    for _k in (str(_code), _name.upper(), _abbr):
        _STATE_LOOKUP[_k] = _code

# The columns of your crash file. Only date and either coordinates or (county + time/victim) are needed.
FIELDS = ["id", "state", "date", "time", "latitude", "longitude", "county", "victim_age", "victim_sex"]
GUESSES = {
    "id": ["crash_id", "crashid", "id", "case_id", "sn"],
    "state": ["state", "state_name", "statename", "st"],
    "date": ["crash_date", "crashdate", "date", "crash_dt"],
    "time": ["crash_time", "crashtime", "time"],
    "latitude": ["latitude", "lat", "laltitude", "y"],
    "longitude": ["longitude", "lon", "long", "lng", "x"],
    "county": ["county", "cnty_id", "county_name", "countyname", "cnty"],
    "victim_age": ["ped_prsn_age", "victim_age", "ped_age", "prsn_age", "age"],
    "victim_sex": ["ped_prsn_gndr_id", "victim_sex", "ped_sex", "prsn_gndr_id", "sex", "gender"],
}


def guess_columns(columns):
    """Best guess for each field from the column names of a crash file."""
    low = {str(c).strip().lower(): c for c in columns}
    out = {}
    for field, names in GUESSES.items():
        out[field] = next((low[n] for n in names if n in low), None)
    return out


# ---------------------------------------------------------------- small helpers
def state_code(v):
    if pd.isna(v):
        return np.nan
    s = str(v).strip().upper()
    if s.endswith(".0"):
        s = s[:-2]
    return _STATE_LOOKUP.get(s, np.nan)


def norm_county(v):
    """'De Witt' -> 'DEWITT', 'BEXAR (29)' -> 'BEXAR', 'Bexar County' -> 'BEXAR'."""
    if pd.isna(v):
        return None
    s = str(v).upper()
    s = re.sub(r"\(.*?\)", "", s).replace(" COUNTY", "").replace(" PARISH", "")
    s = re.sub(r"[^A-Z]", "", s)
    return s or None


def norm_sex(v):
    if pd.isna(v):
        return None
    s = str(v).strip().upper()
    if s in ("1", "1.0", "M", "MALE"):
        return "M"
    if s in ("2", "2.0", "F", "FEMALE"):
        return "F"
    return None


def norm_age(v):
    try:
        a = float(v)
    except (TypeError, ValueError):
        return np.nan
    return a if 0 <= a < 120 else np.nan


def minutes(v):
    """Minutes after midnight from '6:50 PM', '18:50', '1850', or a datetime. NaN if unknown."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return np.nan
    if isinstance(v, pd.Timestamp) or hasattr(v, "hour"):
        return v.hour * 60 + v.minute
    s = str(v).strip().upper()
    for fmt in ("%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S"):
        try:
            t = pd.to_datetime(s, format=fmt)
            return t.hour * 60 + t.minute
        except (ValueError, TypeError):
            pass
    if s.isdigit() and len(s) in (3, 4):
        h, m = int(s[:-2]), int(s[-2:])
        if h < 24 and m < 60:
            return h * 60 + m
    return np.nan


def time_gap(a, b):
    """Smallest gap in minutes between two clock times (wraps at midnight)."""
    d = abs(a - b)
    return min(d, 1440 - d)


def distance_m(lat, lon, lats, lons):
    """Distance in metres from one point to many (equirectangular, fine below a few km)."""
    kx = 111_320 * np.cos(np.radians(lat))
    return np.hypot((lons - lon) * kx, (lats - lat) * 110_540)


# ---------------------------------------------------------------- FARS
def _read(zf, stem):
    names = {n.lower().rsplit("/", 1)[-1]: n for n in zf.namelist()}
    n = names.get(stem + ".csv")
    if n is None:
        return None
    t = pd.read_csv(zf.open(n), encoding="latin-1", low_memory=False, keep_default_na=False, na_values=[""])
    t.columns = [c.replace("ï»¿", "").replace("﻿", "").strip() for c in t.columns]
    return t


def load_fars(fars_dir, states=None, years=None, pedestrian_only=True, log=print):
    """One row per FARS case: state, date, time, place, county, and the people killed.

    fars_dir holds the NHTSA national files FARS<year>NationalCSV.zip.
    pedestrian_only keeps only cases in which a pedestrian died (PER_TYP 5, INJ_SEV 4), and
    the victims are those pedestrians. Otherwise every case is kept and the victims are all
    people killed.
    """
    zips = sorted(glob.glob(os.path.join(fars_dir, "FARS*NationalCSV.zip")))
    if not zips:
        raise SystemExit(f"No FARS<year>NationalCSV.zip files found in {fars_dir}")
    cases, county_names = [], {}
    for z in zips:
        year = int(re.search(r"FARS(\d{4})", os.path.basename(z)).group(1))
        if years and year not in years:
            continue
        with zipfile.ZipFile(z) as zf:
            a = _read(zf, "accident")
            p = _read(zf, "person")
        if states:
            a = a[a["STATE"].isin(states)]
            p = p[p["STATE"].isin(states)]
        if "COUNTYNAME" in a:
            for s, c, n in a[["STATE", "COUNTY", "COUNTYNAME"]].drop_duplicates().itertuples(index=False):
                county_names[(s, c)] = n
        killed = p[p["INJ_SEV"] == 4]
        if pedestrian_only:
            killed = killed[killed["PER_TYP"] == 5]
        vic = killed.groupby("ST_CASE").apply(
            lambda g: [(norm_age(x), norm_sex(y)) for x, y in zip(g["AGE"], g.get("SEXNAME", g["SEX"]))])
        a = a[a["ST_CASE"].isin(vic.index)] if pedestrian_only else a
        a = a.assign(YEAR=year, VICTIMS=a["ST_CASE"].map(vic))
        cases.append(a[["STATE", "ST_CASE", "YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "COUNTY",
                        "LATITUDE", "LONGITUD", "VICTIMS"]])
        log(f"  FARS {year}: {len(a):,} cases")
    f = pd.concat(cases, ignore_index=True)
    f["DATE"] = pd.to_datetime(dict(year=f["YEAR"], month=f["MONTH"], day=f["DAY"]), errors="coerce")
    f["TIME_MIN"] = np.where(f["HOUR"].between(0, 23) & f["MINUTE"].between(0, 59),
                             f["HOUR"] * 60 + f["MINUTE"], np.nan)
    ok = f["LATITUDE"].between(-90, 90) & ~f["LATITUDE"].isin([77.7777, 88.8888, 99.9999])
    f["LAT"] = f["LATITUDE"].where(ok)
    f["LON"] = f["LONGITUD"].where(ok)
    f["COUNTY_KEY"] = [norm_county(county_names.get((s, c))) for s, c in zip(f["STATE"], f["COUNTY"])]
    f["VICTIMS"] = f["VICTIMS"].apply(lambda v: v if isinstance(v, list) else [])
    return f


# ---------------------------------------------------------------- crash file
def read_table(path, sheet=None):
    if path.lower().endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(path, sheet_name=sheet or 0)
    return pd.read_csv(path, low_memory=False)


def prepare_crashes(df, cols, fixed_state=None):
    """Standard columns from the user's crash file. cols maps FIELDS to column names (or None)."""
    g = lambda k: df[cols[k]] if cols.get(k) else pd.Series([np.nan] * len(df), index=df.index)
    c = pd.DataFrame(index=df.index)
    c["STATE"] = state_code(fixed_state) if fixed_state else g("state").map(state_code)
    dt = pd.to_datetime(g("date"), errors="coerce")
    c["DATE"] = dt.dt.normalize()
    # a clock time inside the date column counts only where that state's rows are not all midnight
    clock = (dt.dt.hour * 60 + dt.dt.minute).astype(float)
    share = clock.gt(0).groupby(c["STATE"]).transform("mean")
    clock = clock.where(share > 0.2)
    c["TIME_MIN"] = g("time").map(minutes).fillna(clock) if cols.get("time") else clock
    # 00:00 is a common placeholder for an unknown time
    c.loc[c["TIME_MIN"] == 0, "TIME_MIN"] = np.nan
    lat = pd.to_numeric(g("latitude"), errors="coerce")
    lon = pd.to_numeric(g("longitude"), errors="coerce")
    ok = lat.between(15, 75) & lon.between(-180, -60)
    c["LAT"], c["LON"] = lat.where(ok), lon.where(ok)
    c["COUNTY_KEY"] = g("county").map(norm_county)
    c["AGE"] = g("victim_age").map(norm_age)
    c["SEX"] = g("victim_sex").map(norm_sex)
    return c


# ---------------------------------------------------------------- the two steps
def victim_agrees(age, sex, victims):
    """True / False / None (cannot tell) for a crash victim against a FARS case's victims."""
    if not victims or (np.isnan(age) and sex is None):
        return None
    for fa, fs in victims:
        a_ok = None if (np.isnan(age) or np.isnan(fa)) else abs(age - fa) <= AGE_TOL
        s_ok = None if (sex is None or fs is None) else sex == fs
        if a_ok is not False and s_ok is not False and (a_ok or s_ok):
            return True
    if all((np.isnan(age) or np.isnan(fa)) and (sex is None or fs is None) for fa, fs in victims):
        return None
    return False


def step1_location(c, by_day):
    """Nearest FARS case on the same day in the same state."""
    if np.isnan(c.LAT):
        return None
    cand = by_day.get((c.STATE, c.DATE))
    if cand is None:
        return None
    cand = cand[cand["LAT"].notna()]
    if cand.empty:
        return None
    d = distance_m(c.LAT, c.LON, cand["LAT"].values, cand["LON"].values)
    o = np.argsort(d)
    d1 = d[o[0]]
    d2 = d[o[1]] if len(d) > 1 else np.inf
    if d1 < NEAR_M:
        return cand.index[o[0]], "1A_NEAREST_UNDER_150M", d1
    if d1 < FAR_M and d2 >= FAR_M:
        return cand.index[o[0]], "1B_ONLY_CASE_UNDER_1KM", d1
    return None


def step2_attributes(c, by_day):
    """The one FARS case on the same day that agrees on county, time and victim."""
    cand = by_day.get((c.STATE, c.DATE))
    if cand is None:
        return None
    hits = []
    for i, f in cand.iterrows():
        checks = []
        if c.COUNTY_KEY and f.COUNTY_KEY:
            checks.append(c.COUNTY_KEY == f.COUNTY_KEY)
        if not np.isnan(c.TIME_MIN) and not np.isnan(f.TIME_MIN):
            checks.append(time_gap(c.TIME_MIN, f.TIME_MIN) <= TIME_MIN)
        v = victim_agrees(c.AGE, c.SEX, f.VICTIMS)
        if v is not None:
            checks.append(v)
        if len(checks) >= 2 and all(checks):
            hits.append(i)
    if len(hits) == 1:
        return hits[0], "2_COUNTY_TIME_VICTIM", np.nan
    return None


def confirm(c, f):
    """Check a match on the fields that were not needed to make it."""
    notes, bad = [], False
    if c.COUNTY_KEY and f.COUNTY_KEY:
        if c.COUNTY_KEY == f.COUNTY_KEY:
            notes.append("county agrees")
        else:
            notes.append("county differs"); bad = True
    gap = np.nan
    if not np.isnan(c.TIME_MIN) and not np.isnan(f.TIME_MIN):
        gap = time_gap(c.TIME_MIN, f.TIME_MIN)
        if gap <= TIME_MIN:
            notes.append("time agrees")
        elif abs(abs(c.TIME_MIN - f.TIME_MIN) - 720) <= 5:
            notes.append("time off by 12 h (AM/PM)")
        else:
            notes.append("time differs"); bad = True
    v = victim_agrees(c.AGE, c.SEX, f.VICTIMS)
    if v is True:
        notes.append("victim agrees")
    elif v is False:
        notes.append("victim differs"); bad = True
    status = "NOT_CHECKED" if not notes else ("CONFLICT" if bad else "CONFIRMED")
    return status, "; ".join(notes), gap


def match(crashes, fars, log=print):
    """Return one row per crash with the FARS case found and how it was found."""
    by_day = {k: g for k, g in fars.groupby(["STATE", "DATE"])}
    rows = []
    for i, c in crashes.iterrows():
        r = dict(FARS_YEAR=np.nan, FARS_ST_CASE=np.nan, MATCH_RULE="NO_MATCH", MATCH_DIST_M=np.nan,
                 CHECK="", CHECK_NOTES="", TIME_GAP_MIN=np.nan)
        if pd.isna(c.STATE) or pd.isna(c.DATE):
            r["MATCH_RULE"] = "NO_MATCH_MISSING_STATE_OR_DATE"
        else:
            hit = step1_location(c, by_day) or step2_attributes(c, by_day)
            if hit:
                j, rule, d = hit
                f = fars.loc[j]
                r.update(FARS_YEAR=int(f.YEAR), FARS_ST_CASE=int(f.ST_CASE), MATCH_RULE=rule,
                         MATCH_DIST_M=round(d) if not np.isnan(d) else np.nan)
                r["CHECK"], r["CHECK_NOTES"], r["TIME_GAP_MIN"] = confirm(c, f)
            elif c.DATE.year not in set(fars["YEAR"]):
                r["MATCH_RULE"] = "NO_MATCH_YEAR_NOT_IN_FARS"
        rows.append(r)
    out = pd.DataFrame(rows, index=crashes.index)
    key = out["FARS_ST_CASE"].notna()
    dup = out[key].duplicated(["FARS_YEAR", "FARS_ST_CASE"], keep=False)
    out["SHARED_FARS_CASE"] = False
    out.loc[dup[dup].index, "SHARED_FARS_CASE"] = True
    for c in ("FARS_YEAR", "FARS_ST_CASE"):
        out[c] = out[c].astype("Int64")
    return out


def summary(out, state=None):
    s = out.assign(STATE=state) if state is not None else out.assign(STATE="All")
    t = pd.crosstab(s["STATE"], s["MATCH_RULE"], margins=True, margins_name="Total")
    k = pd.crosstab(s["MATCH_RULE"], s["CHECK"].replace("", "-"), margins=True, margins_name="Total")
    return t, k


# ---------------------------------------------------------------- run
def run(crashes_path, fars_dir, out_path, cols=None, fixed_state=None, sheet=None,
        pedestrian_only=True, log=print):
    log(f"fars_match {__version__}")
    df = read_table(crashes_path, sheet)
    cols = cols or guess_columns(df.columns)
    log("Columns used: " + ", ".join(f"{k}={v}" for k, v in cols.items() if v))
    c = prepare_crashes(df, cols, fixed_state)
    log(f"Crashes: {len(c):,}   with coordinates: {c['LAT'].notna().sum():,}   with time: "
        f"{c['TIME_MIN'].notna().sum():,}   with county: {c['COUNTY_KEY'].notna().sum():,}   with victim: "
        f"{(c['AGE'].notna() | c['SEX'].notna()).sum():,}")
    states = sorted(int(s) for s in c["STATE"].dropna().unique())
    years = sorted(int(y) for y in c["DATE"].dt.year.dropna().unique())
    log(f"Reading FARS for states {states}, years {years[0] if years else '-'}-{years[-1] if years else '-'} ...")
    f = load_fars(fars_dir, states=states, years=years, pedestrian_only=pedestrian_only, log=log)
    log("Matching ...")
    m = match(c, f, log=log)
    res = pd.concat([df.reset_index(drop=True), m.reset_index(drop=True)], axis=1)
    state_names = c["STATE"].map(lambda s: STATES.get(int(s), ("?",))[0] if pd.notna(s) else "?").values
    t, k = summary(m.reset_index(drop=True), state_names)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    if out_path.lower().endswith(".csv"):
        res.to_csv(out_path, index=False)
        t.to_csv(out_path[:-4] + "_summary.csv")
    else:
        with pd.ExcelWriter(out_path, engine="openpyxl") as w:
            res.to_excel(w, sheet_name="Matched", index=False)
            t.to_excel(w, sheet_name="Summary")
            k.to_excel(w, sheet_name="Summary", startrow=len(t) + 3)
    log("\n" + t.to_string())
    log("\n" + k.to_string())
    log(f"\nWrote {out_path}")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="Find the FARS case for each crash in a state crash file.")
    ap.add_argument("--crashes", required=True, help="crash file (.xlsx or .csv)")
    ap.add_argument("--fars", required=True, help="folder with FARS<year>NationalCSV.zip files")
    ap.add_argument("--out", required=True, help="output file (.xlsx or .csv)")
    ap.add_argument("--state", help="use this state for every row (name, postal code or FIPS)")
    ap.add_argument("--all-fatal", action="store_true", help="match to any fatal crash, not only pedestrian deaths")
    for k in FIELDS:
        ap.add_argument(f"--{k.replace('_', '-')}-col", dest=k, help=f"column holding {k} (guessed if left out)")
    a = ap.parse_args(argv)
    df_cols = read_table(a.crashes).columns
    cols = guess_columns(df_cols)
    for k in FIELDS:
        if getattr(a, k):
            cols[k] = getattr(a, k)
    run(a.crashes, a.fars, a.out, cols=cols, fixed_state=a.state, pedestrian_only=not a.all_fatal)


if __name__ == "__main__":
    main()
