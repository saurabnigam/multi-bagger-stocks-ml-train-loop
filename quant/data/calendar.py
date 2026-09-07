from __future__ import annotations

import bisect
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd

from quant.errors import Blocked


class Calendar:
    def __init__(self, sessions: pd.DataFrame, timezone: str = "Asia/Kolkata"):
        if "date" not in sessions.columns or "close_at" not in sessions.columns:
            raise ValueError("sessions DataFrame must have 'date' and 'close_at' columns")

        sorted_df = sessions.sort_values("date").reset_index(drop=True)
        self._df = sorted_df
        self._dates = list(sorted_df["date"])
        self._close_ats = dict(zip(sorted_df["date"], sorted_df["close_at"]))
        self.timezone = timezone

    def last_session_on_or_before(self, date: str) -> str:
        idx = bisect.bisect_right(self._dates, date) - 1
        if idx < 0:
            raise Blocked("calendar_missing", f"No session on or before {date}")
        return self._dates[idx]

    def next_session_after(self, date: str) -> str:
        idx = bisect.bisect_right(self._dates, date)
        if idx >= len(self._dates):
            raise Blocked("calendar_missing", f"No session strictly after {date}")
        return self._dates[idx]

    def add_sessions(self, date: str, count: int) -> str:
        if date in self._dates:
            curr_idx = self._dates.index(date)
        else:
            if count > 0:
                curr_idx = bisect.bisect_right(self._dates, date)
                count -= 1
            else:
                curr_idx = bisect.bisect_left(self._dates, date) - 1

        target_idx = curr_idx + count
        if target_idx < 0 or target_idx >= len(self._dates):
            raise Blocked(
                "calendar_missing",
                f"Session shift from {date} by {count} exceeds available session range",
            )
        return self._dates[target_idx]

    def month_ends(self, start: str, end: str) -> list[str]:
        # Month-end is the last completed session in each month
        # Group sessions by month YYYY-MM
        result = []
        df_range = self._df[(self._df["date"] >= start) & (self._df["date"] <= end)].copy()
        if df_range.empty:
            return []

        df_range["month"] = df_range["date"].str[:7]
        # For each month in the range, find the maximum session date in that month
        # from the full sessions table
        months = sorted(df_range["month"].unique())
        for m in months:
            m_sessions = self._df[self._df["date"].str.startswith(m)]
            last_date = m_sessions["date"].max()
            if start <= last_date <= end:
                result.append(last_date)
        return result

    def cutoff(self, as_of: str) -> str:
        # Knowledge cutoff is as_of at 23:59:59.999999 Asia/Kolkata converted to UTC
        dt_local = datetime.fromisoformat(f"{as_of}T23:59:59.999999").replace(
            tzinfo=ZoneInfo(self.timezone)
        )
        dt_utc = dt_local.astimezone(timezone.utc)
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def first_exec_after(self, generated_at: str) -> str:
        # Returns the close_at timestamp of the first session strictly AFTER the IST date of generated_at
        cleaned = generated_at.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt_gen = datetime.fromisoformat(cleaned)
        if dt_gen.tzinfo is None:
            dt_gen = dt_gen.replace(tzinfo=timezone.utc)

        dt_local = dt_gen.astimezone(ZoneInfo(self.timezone))
        local_date = dt_local.strftime("%Y-%m-%d")

        next_session_date = self.next_session_after(local_date)
        return self._close_ats[next_session_date]
