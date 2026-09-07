"""Restricted point-in-time FactorInputs container."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
import numpy as np
import pandas as pd

from quant.data.prices import PriceStore
from quant.errors import LookaheadError, Refused
from quant.run import RunContext
from quant.types import Draft

ALLOWED_ATTRIBUTES = {
    "market_cap_inr",
    "shares_outstanding",
    "dividend_rate_inr",
    "dividend_yield_frac",
    "debt_to_equity_x",
    "pe_ratio",
    "roe_frac",
    "roce_frac",
    "inst_held_frac",
    "insider_held_frac",
}

ALLOWED_STATEMENTS = {
    "balance_sheet",
    "financials",
    "income_statement",
    "cashflow",
}


class FactorInputs:
    """Restricted container providing point-in-time data for factor calculation.
    
    Exposes no database connection, network client or label frames.
    All accesses are strictly capped at as_of date and knowledge_cutoff timestamp.
    """

    def __init__(
        self,
        as_of: str,
        cutoff: str,
        members: pd.Index,
        sector_group: pd.Series,
        price_store: PriceStore,
        state_query_fn: Callable[[str, tuple], List[tuple]],
        provenance_dict: Dict[str, Any],
    ):
        self.as_of = str(as_of)
        self.cutoff = str(cutoff)
        self.members = members
        self.sector_group = sector_group
        self._price_store = price_store
        self._query = state_query_fn
        self._provenance = provenance_dict

    def _get_start_date(self, lookback_days: int) -> str:
        if lookback_days < 0:
            raise LookaheadError(f"Negative lookback_days ({lookback_days}) requests future data")
        dt = datetime.strptime(self.as_of, "%Y-%m-%d") - timedelta(days=int(lookback_days * 3.0) + 90)
        return dt.strftime("%Y-%m-%d")

    def tri(self, lookback_days: int) -> pd.DataFrame:
        """Point-in-time Total Return Index (TRI) series."""
        start = self._get_start_date(lookback_days)
        sids = [int(x) for x in self.members]
        return self._price_store.tri(sids, start=start, end=self.as_of, vintage_at=self.cutoff)

    def close_split(self, lookback_days: int) -> pd.DataFrame:
        """Point-in-time split-adjusted close series rebased to end date."""
        start = self._get_start_date(lookback_days)
        sids = [int(x) for x in self.members]
        return self._price_store.close_split(sids, start=start, end=self.as_of, vintage_at=self.cutoff)

    def close_raw(self, lookback_days: int) -> pd.DataFrame:
        """Point-in-time raw closing price series."""
        start = self._get_start_date(lookback_days)
        sids = [int(x) for x in self.members]
        return self._price_store.close_raw(sids, start=start, end=self.as_of, vintage_at=self.cutoff)

    def volume(self, lookback_days: int) -> pd.DataFrame:
        """Point-in-time raw trading volume series."""
        start = self._get_start_date(lookback_days)
        sids = [int(x) for x in self.members]
        return self._price_store.volume(sids, start=start, end=self.as_of, vintage_at=self.cutoff)

    def attribute(self, field: str) -> pd.Series:
        """Point-in-time cross-sectional security attribute."""
        attr_map = {
            "market_cap_inr": "mcap_inr",
            "mcap_inr": "mcap_inr",
            "shares_outstanding": "shares_out",
            "shares_out": "shares_out",
            "float_shares": "float_shares",
            "ev_inr": "ev_inr",
            "trailing_pe": "trailing_pe",
            "pe_ratio": "trailing_pe",
            "price_to_book": "price_to_book",
            "dividend_rate_inr": "dividend_rate_inr",
            "beta": "beta",
            "yahoo_sector": "yahoo_sector",
            "yahoo_industry": "yahoo_industry",
        }
        col = attr_map.get(field)
        if not col:
            raise LookaheadError(f"Undeclared or unknown attribute field requested: {field}")

        query = f"""
        WITH ranked AS (
            SELECT security_id, {col} as val,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY captured_at DESC) as rn
            FROM security_attributes
            WHERE captured_at <= ?
        )
        SELECT security_id, val
        FROM ranked
        WHERE rn = 1
        """
        rows = self._query(query, (self.cutoff,))
        res = {r[0]: float(r[1]) if r[1] is not None else np.nan for r in rows}
        return pd.Series(res).reindex(self.members)

    def fundamental(self, statement: str, field: str, freq: str, n_periods: int) -> pd.DataFrame:
        """Point-in-time fundamental statement series."""
        if statement not in ALLOWED_STATEMENTS:
            raise LookaheadError(f"Undeclared statement requested: {statement}")

        query = """
        WITH ranked AS (
            SELECT security_id, period_end, value,
                   ROW_NUMBER() OVER (PARTITION BY security_id, period_end ORDER BY observed_at DESC) as rn
            FROM fundamentals_pit
            WHERE statement = ?
              AND field = ?
              AND freq = ?
              AND period_end <= ?
              AND observed_at <= ?
        )
        SELECT security_id, period_end, value
        FROM ranked
        WHERE rn = 1
        ORDER BY period_end DESC
        """
        rows = self._query(query, (statement, field, freq, self.as_of, self.cutoff))
        if not rows:
            return pd.DataFrame(index=self.members)

        df = pd.DataFrame(rows, columns=["security_id", "period_end", "value"])
        pivoted = df.pivot(index="security_id", columns="period_end", values="value")
        return pivoted.reindex(index=self.members)

    def ttm(self, field: str, offset_quarters: int = 0) -> pd.Series:
        """Point-in-time trailing twelve month sum from quarterly statements."""
        if offset_quarters < 0:
            raise LookaheadError(f"Negative offset_quarters ({offset_quarters}) requests future data")

        # Query up to 8 quarters to handle offset
        query = """
        WITH ranked AS (
            SELECT security_id, period_end, value,
                   ROW_NUMBER() OVER (PARTITION BY security_id, period_end ORDER BY observed_at DESC) as rn
            FROM fundamentals_pit
            WHERE field = ?
              AND freq = 'quarterly'
              AND period_end <= ?
              AND observed_at <= ?
        )
        SELECT security_id, period_end, value
        FROM ranked
        WHERE rn = 1
        ORDER BY period_end DESC
        """
        rows = self._query(query, (field, self.as_of, self.cutoff))
        if not rows:
            return pd.Series(np.nan, index=self.members)

        df = pd.DataFrame(rows, columns=["security_id", "period_end", "value"])
        out = {}
        for sid, group in df.groupby("security_id"):
            g = group.sort_values("period_end", ascending=False)
            # Apply offset
            sub = g.iloc[offset_quarters:offset_quarters + 4]
            if len(sub) == 4:
                out[sid] = float(sub["value"].sum())
            else:
                out[sid] = np.nan

        return pd.Series(out).reindex(self.members)

    def holdings(self, lag_runs: int = 0) -> pd.Series:
        """Point-in-time institutional holdings share."""
        if lag_runs < 0:
            raise LookaheadError(f"Negative lag_runs ({lag_runs}) requests future holdings")

        query = """
        WITH ranked AS (
            SELECT security_id, inst_pct,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY observed_at DESC) as rn
            FROM holdings_history
            WHERE observed_at <= ?
        )
        SELECT security_id, inst_pct
        FROM ranked
        WHERE rn = 1
        """
        rows = self._query(query, (self.cutoff,))
        res = {r[0]: float(r[1]) if r[1] is not None else np.nan for r in rows}
        return pd.Series(res).reindex(self.members)

    def adv_inr(self) -> pd.Series:
        """Average daily turnover in INR over trailing 63 trading days."""
        sids = [int(x) for x in self.members]
        df = self._price_store.adv_inr(sids, as_of=self.as_of, vintage_at=self.cutoff, window=63)
        return df["adv_63_inr"].reindex(self.members)

    def benchmark_tri(self, symbol: str, lookback_days: int) -> pd.Series:
        """Point-in-time benchmark TRI series."""
        start = self._get_start_date(lookback_days)
        query = """
        WITH ranked AS (
            SELECT month_end, tri,
                   ROW_NUMBER() OVER (PARTITION BY month_end ORDER BY observed_at DESC) as rn
            FROM benchmarks_monthly
            WHERE benchmark_id = ?
              AND month_end >= ?
              AND month_end <= ?
              AND observed_at <= ?
        )
        SELECT month_end, tri
        FROM ranked
        WHERE rn = 1
        ORDER BY month_end ASC
        """
        rows = self._query(query, (symbol, start, self.as_of, self.cutoff))
        if not rows:
            return pd.Series(dtype=float, name=symbol)
        dates = [r[0] for r in rows]
        tris = [float(r[1]) for r in rows]
        return pd.Series(tris, index=dates, name=symbol)

    def provenance(self) -> Dict[str, Any]:
        """Dictionary of exact observation references, hashes and timestamps."""
        return {
            "as_of": self.as_of,
            "cutoff": self.cutoff,
            "n_members": len(self.members),
            **self._provenance,
        }


def build(ctx: RunContext, draft: Draft) -> FactorInputs:
    """Construct a restricted FactorInputs container from RunContext and Draft."""
    store = getattr(ctx, "store", None) or PriceStore(ctx.cfg.paths.prices_db, state_conn=ctx.conn)
    sids = sorted([int(x) for x in draft.members["security_id"].unique()])
    members_idx = pd.Index(sids, name="security_id")
    sector_group = draft.groups.reindex(members_idx) if draft.groups is not None else pd.Series(index=members_idx, dtype=object)

    def _state_query(query: str, params: tuple) -> List[tuple]:
        cur = ctx.conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()

    prov = {
        "cohort_id": draft.cohort_id,
        "track": draft.track,
        "definition_hash": draft.definition_hash,
        "price_manifest_sha": draft.source_refs.get("price_manifest_sha", ""),
    }

    return FactorInputs(
        as_of=draft.as_of,
        cutoff=draft.knowledge_cutoff,
        members=members_idx,
        sector_group=sector_group,
        price_store=store,
        state_query_fn=_state_query,
        provenance_dict=prov,
    )
