from __future__ import annotations

import bisect
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd

from quant.errors import Blocked


SESSION_CLOSE_UTC = "T10:00:00.000000Z"   # 15:30 IST


def weekday_sessions(start: str, end: str) -> pd.DataFrame:
    """Weekday fallback session table (flagged; NSE holidays are not removed)."""
    dates = pd.bdate_range(start, end)
    return pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in dates],
        "close_at": [f"{d.strftime('%Y-%m-%d')}{SESSION_CLOSE_UTC}" for d in dates],
        "source": "weekday_fallback",
    })


class Calendar:
    def __init__(self, sessions: pd.DataFrame, timezone: str = "Asia/Kolkata"):
        if "date" not in sessions.columns or "close_at" not in sessions.columns:
            raise ValueError("sessions DataFrame must have 'date' and 'close_at' columns")

        sorted_df = sessions.sort_values("date").drop_duplicates("date").reset_index(drop=True)
        self._df = sorted_df
        self._dates = list(sorted_df["date"])
        self._close_ats = dict(zip(sorted_df["date"], sorted_df["close_at"]))
        self.timezone = timezone
        self.sources: dict[str, int] = (
            sorted_df["source"].value_counts().to_dict() if "source" in sorted_df.columns else {}
        )

    @classmethod
    def load(cls, cfg, store=None, *, horizon_years: int = 3) -> "Calendar":
        """Build the session calendar from, in order of authority:

        1. the configured sessions file (verified sessions, if present);
        2. empirical sessions observed in the price store (dates with closes);
        3. a weekday fallback for the remainder, flagged ``weekday_fallback``.
        Callers can inspect ``calendar.sources`` to report fallback usage.
        """
        import os
        from datetime import date

        frames = []
        sessions_file = getattr(getattr(cfg, "calendar", None), "sessions_file", None)
        root = getattr(cfg, "root", None)
        if sessions_file:
            path = sessions_file if os.path.isabs(str(sessions_file)) or root is None else os.path.join(str(root), str(sessions_file))
            if os.path.exists(path):
                df = pd.read_csv(path, dtype=str)
                if "close_at" not in df.columns:
                    df["close_at"] = df["date"] + SESSION_CLOSE_UTC
                df["source"] = "sessions_file"
                frames.append(df[["date", "close_at", "source"]])
        start = str(getattr(getattr(cfg, "yahoo", None), "history_start", "2015-01-01"))
        end = f"{date.today().year + horizon_years}-12-31"
        if store is not None:
            try:
                emp = store.session_dates(start, end, min_securities=10)
            except Exception:
                emp = []
            if emp:
                frames.append(pd.DataFrame({
                    "date": emp, "close_at": [d + SESSION_CLOSE_UTC for d in emp], "source": "price_store",
                }))
        known = set()
        for f in frames:
            known.update(f["date"].tolist())
        fallback = weekday_sessions(start, end)
        fallback = fallback[~fallback["date"].isin(known)]
        # Weekdays inside the empirically observed range without closes are holidays, not sessions.
        if store is not None and any(f["source"].iloc[0] == "price_store" for f in frames if len(f)):
            emp_dates = sorted(d for f in frames if f["source"].iloc[0] == "price_store" for d in f["date"])
            fallback = fallback[(fallback["date"] < emp_dates[0]) | (fallback["date"] > emp_dates[-1])]
        frames.append(fallback)
        return cls(pd.concat(frames, ignore_index=True))

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
