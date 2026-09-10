"""Restricted point-in-time FactorInputs container."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
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
    "balance",
    "balance_sheet",
    "financials",
    "income",
    "income_statement",
    "cashflow",
    "info",
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
        fund_fn: Optional[Callable[[str, str, str, int, List[int]], pd.DataFrame]] = None,
        ttm_fn: Optional[Callable[[str, int, List[int]], Tuple[pd.Series, pd.Series]]] = None,
        holdings_fn: Optional[Callable[[int, List[int]], pd.Series]] = None,
    ):
        self.as_of = str(as_of)
        self.cutoff = str(cutoff)
        self.members = members
        self.sector_group = sector_group
        self._price_store = price_store
        self._query = state_query_fn
        self._provenance = provenance_dict
        self._fund_fn = fund_fn
        self._ttm_fn = ttm_fn
        self._holdings_fn = holdings_fn

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
        stmt_map = {
            "income": "income",
            "income_statement": "income",
            "financials": "income",
            "balance": "balance",
            "balance_sheet": "balance",
            "cashflow": "cashflow",
            "info": "info",
        }
        if statement not in stmt_map and statement not in ALLOWED_STATEMENTS:
            raise LookaheadError(f"Undeclared statement requested: {statement}")

        canon_stmt = stmt_map.get(statement, statement)
        freq_norm = freq.upper()
        if freq_norm in ("ANNUAL", "A"):
            freq_norm = "A"
        elif freq_norm in ("QUARTERLY", "Q"):
            freq_norm = "Q"
        elif freq_norm in ("POINT", "P"):
            freq_norm = "P"

        sids = [int(x) for x in self.members]
        if self._fund_fn:
            df = self._fund_fn(canon_stmt, field, freq_norm, n_periods, sids)
            return df.reindex(index=self.members)

        from quant.data.fundamentals import _expand_fields
        fields = _expand_fields(field)
        placeholders = ",".join("?" for _ in fields)
        query = f"""
        WITH ranked AS (
            SELECT security_id, period_end, value, fetched_at,
                   ROW_NUMBER() OVER (PARTITION BY security_id, period_end ORDER BY fetched_at DESC) as rn
            FROM fundamentals
            WHERE statement = ?
              AND field IN ({placeholders})
              AND freq = ?
              AND available_from <= ?
              AND fetched_at <= ?
        )
        SELECT security_id, period_end, value
        FROM ranked
        WHERE rn = 1
        ORDER BY period_end DESC
        """
        rows = self._query(query, (canon_stmt, *fields, freq_norm, self.cutoff, self.cutoff))
        if not rows:
            return pd.DataFrame(index=self.members, columns=list(range(n_periods)))

        df_rows = pd.DataFrame(rows, columns=["security_id", "period_end", "value"])
        data = {sid: [np.nan] * n_periods for sid in self.members}
        for sid, grp in df_rows.groupby("security_id"):
            sorted_v = grp.sort_values("period_end", ascending=False)["value"].tolist()
            for r, v in enumerate(sorted_v[:n_periods]):
                data[sid][r] = v
        return pd.DataFrame.from_dict(data, orient="index", columns=list(range(n_periods)))

    def ttm(self, field: str, offset_quarters: int = 0) -> pd.Series:
        """Point-in-time trailing twelve month sum from quarterly statements."""
        if offset_quarters < 0:
            raise LookaheadError(f"Negative offset_quarters ({offset_quarters}) requests future data")

        sids = [int(x) for x in self.members]
        if self._ttm_fn:
            vals, _ = self._ttm_fn(field, offset_quarters, sids)
            return vals.reindex(self.members)

        return pd.Series(np.nan, index=self.members)

    def holdings(self, lag_runs: int = 0) -> pd.Series:
        """Point-in-time institutional holdings share."""
        if lag_runs < 0:
            raise LookaheadError(f"Negative lag_runs ({lag_runs}) requests future holdings")

        sids = [int(x) for x in self.members]
        if self._holdings_fn:
            ser = self._holdings_fn(lag_runs, sids)
            return ser.reindex(self.members)

        query = """
        WITH ranked AS (
            SELECT security_id, inst_pct,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY captured_at DESC) as rn
            FROM holdings
            WHERE captured_at <= ?
        )
        SELECT security_id, inst_pct
        FROM ranked
        WHERE rn = ?
        """
        rows = self._query(query, (self.cutoff, lag_runs + 1))
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

    def _fund_fn(statement: str, field: str, freq: str, n_periods: int, sids: List[int]) -> pd.DataFrame:
        from quant.data.fundamentals import pit_frame
        return pit_frame(ctx.conn, draft.knowledge_cutoff, statement, field, freq, n_periods, sids)

    def _ttm_fn(field: str, offset_quarters: int, sids: List[int]) -> Tuple[pd.Series, pd.Series]:
        from quant.data.fundamentals import ttm
        return ttm(ctx.conn, draft.knowledge_cutoff, field, sids, offset_quarters=offset_quarters)

    def _holdings_fn(lag_runs: int, sids: List[int]) -> pd.Series:
        from quant.data.holdings import series
        return series(ctx.conn, draft.knowledge_cutoff, lag_runs, sids)

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
        fund_fn=_fund_fn,
        ttm_fn=_ttm_fn,
        holdings_fn=_holdings_fn,
    )
