import json
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import pytest

from quant.data.calendar import Calendar
from quant.errors import Blocked


def make_weekday_sessions(start_date: str, end_date: str) -> pd.DataFrame:
    """Helper to generate weekday sessions DataFrame."""
    dt_start = datetime.fromisoformat(start_date)
    dt_end = datetime.fromisoformat(end_date)
    cur = dt_start
    rows = []
    while cur <= dt_end:
        if cur.weekday() < 5:  # Mon-Fri
            d_str = cur.strftime("%Y-%m-%d")
            rows.append({
                "date": d_str,
                "close_at": f"{d_str}T10:00:00.000000Z",
            })
        cur += timedelta(days=1)
    return pd.DataFrame(rows)


def test_calendar_golden_cases():
    cases = json.loads(Path("docs/spec/contracts/golden_cases.json").read_text())["cases"]
    
    # Golden case: pit_cutoff
    c_pit = cases["pit_cutoff"]
    sessions = make_weekday_sessions("2026-01-01", "2026-12-31")
    cal = Calendar(sessions)
    assert cal.cutoff(c_pit["as_of"]) == c_pit["cutoff"]

    # Golden case: execution
    c_exec = cases["execution"]
    df_exec_sessions = pd.DataFrame(c_exec["sessions"])
    cal_exec = Calendar(df_exec_sessions)
    earliest = cal_exec.first_exec_after(c_exec["generated_at"])
    assert earliest == c_exec["expected_earliest_exec_at"]

    # Golden case: calendar_lags
    c_lags = cases["calendar_lags"]
    # 45 days after quarter_end 2026-06-30
    d45 = (datetime.fromisoformat(c_lags["quarter_end"]) + timedelta(days=45)).strftime("%Y-%m-%d")
    assert d45 == c_lags["estimated_45th_day"]
    next_s = cal.last_session_on_or_before(d45)
    # If d45 is Friday 2026-08-14, last session on or before is 2026-08-14
    # The session on or after d45:
    assert (cal.next_session_after(d45) if d45 not in sessions["date"].values else d45) == c_lags["estimated_45th_day"]
    # 60 days after annual_end 2026-03-31 is Saturday 2026-05-30
    d60 = (datetime.fromisoformat(c_lags["annual_end"]) + timedelta(days=60)).strftime("%Y-%m-%d")
    assert d60 == c_lags["estimated_60th_day"]
    # next session strictly after Saturday 2026-05-30 is Monday 2026-06-01
    assert cal.next_session_after(d60) == c_lags["weekday_next_annual_session"]


def test_calendar_session_arithmetic_and_missing_rejection():
    sessions = make_weekday_sessions("2026-09-01", "2026-09-10")
    # Mon-Fri:
    # 2026-09-01 (Tue), 02 (Wed), 03 (Thu), 04 (Fri), 07 (Mon), 08 (Tue), 09 (Wed), 10 (Thu)
    cal = Calendar(sessions)

    # last_session_on_or_before
    assert cal.last_session_on_or_before("2026-09-05") == "2026-09-04"
    assert cal.last_session_on_or_before("2026-09-04") == "2026-09-04"

    # next_session_after
    assert cal.next_session_after("2026-09-04") == "2026-09-07"

    # add_sessions
    assert cal.add_sessions("2026-09-04", 1) == "2026-09-07"
    assert cal.add_sessions("2026-09-07", -1) == "2026-09-04"

    # Out of bounds raises Blocked(calendar_missing)
    with pytest.raises(Blocked) as excinfo:
        cal.next_session_after("2026-09-10")
    assert excinfo.value.code == "calendar_missing"

    with pytest.raises(Blocked) as excinfo:
        cal.last_session_on_or_before("2026-08-31")
    assert excinfo.value.code == "calendar_missing"

    with pytest.raises(Blocked) as excinfo:
        cal.add_sessions("2026-09-01", -1)
    assert excinfo.value.code == "calendar_missing"


def test_calendar_month_ends():
    sessions = make_weekday_sessions("2026-01-01", "2026-03-31")
    cal = Calendar(sessions)
    ends = cal.month_ends("2026-01-01", "2026-03-31")
    assert len(ends) == 3
    # In Jan 2026, 31 is Sat, so 30 is Fri
    assert ends[0] == "2026-01-30"
    # In Feb 2026, 28 is Sat, so 27 is Fri
    assert ends[1] == "2026-02-27"
    # In Mar 2026, 31 is Tue
    assert ends[2] == "2026-03-31"
