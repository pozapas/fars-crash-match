"""Synthetic test of the two matching steps (no real crash or FARS data)."""
import os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fars_match as fm


def fars_cases():
    d = pd.Timestamp("2022-05-01")
    f = pd.DataFrame({
        "STATE": [48, 48, 48, 48], "ST_CASE": [480001, 480002, 480003, 480004], "YEAR": 2022,
        "DATE": [d, d, d, pd.Timestamp("2022-05-02")],
        "TIME_MIN": [18 * 60 + 50, 9 * 60, 21 * 60 + 5, 600],
        "LAT": [30.0000, 30.2000, 30.5000, 30.0000], "LON": [-97.7400, -97.7400, -97.7400, -97.7400],
        "COUNTY_KEY": ["TRAVIS", "TRAVIS", "WILLIAMSON", "TRAVIS"],
        "VICTIMS": [[(45.0, "M")], [(30.0, "F")], [(62.0, "M")], [(20.0, "F")]],
    })
    return f


def crash(**k):
    base = dict(STATE=48, DATE=pd.Timestamp("2022-05-01"), TIME_MIN=np.nan, LAT=np.nan, LON=np.nan,
                COUNTY_KEY=None, AGE=np.nan, SEX=None)
    base.update(k)
    return pd.DataFrame([base])


def test_step1_nearest_under_150m():
    c = crash(LAT=30.0009, LON=-97.7400)            # about 100 m north of case 480001
    m = fm.match(c, fars_cases())
    assert m.FARS_ST_CASE.iloc[0] == 480001 and m.MATCH_RULE.iloc[0] == "1A_NEAREST_UNDER_150M"


def test_step1_only_case_under_1km():
    c = crash(LAT=30.5050, LON=-97.7400)            # about 550 m from 480003, nothing else within 1 km
    m = fm.match(c, fars_cases())
    assert m.FARS_ST_CASE.iloc[0] == 480003 and m.MATCH_RULE.iloc[0] == "1B_ONLY_CASE_UNDER_1KM"


def test_step2_county_time_victim():
    c = crash(COUNTY_KEY="TRAVIS", TIME_MIN=9 * 60 + 20, AGE=31.0, SEX="F")   # no coordinates
    m = fm.match(c, fars_cases())
    assert m.FARS_ST_CASE.iloc[0] == 480002 and m.MATCH_RULE.iloc[0] == "2_COUNTY_TIME_VICTIM"


def test_no_match_when_ambiguous():
    c = crash(COUNTY_KEY="TRAVIS")                   # only one field: not enough for Step 2
    m = fm.match(c, fars_cases())
    assert m.MATCH_RULE.iloc[0] == "NO_MATCH"


def test_check_flags_conflict_and_ampm():
    f = fars_cases().iloc[0]
    ok = crash(LAT=30.0, LON=-97.74, TIME_MIN=6 * 60 + 50, COUNTY_KEY="TRAVIS", AGE=45.0, SEX="M").iloc[0]
    status, notes, _ = fm.confirm(ok, f)
    assert status == "CONFIRMED" and "12 h" in notes
    bad = crash(LAT=30.0, LON=-97.74, COUNTY_KEY="TRAVIS", AGE=70.0, SEX="F").iloc[0]
    assert fm.confirm(bad, f)[0] == "CONFLICT"


def test_helpers():
    assert fm.minutes("06:50 PM") == 18 * 60 + 50 and fm.minutes("1850") == 18 * 60 + 50
    assert fm.norm_county("De Witt") == fm.norm_county("DEWITT (123)") == "DEWITT"
    assert fm.state_code("TX") == fm.state_code("Texas") == fm.state_code(48) == 48
    assert fm.norm_sex(1) == "M" and fm.norm_sex("Female") == "F"
