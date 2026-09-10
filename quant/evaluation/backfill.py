"""Backfill replay and warmup accounting (C07).

Backfill replay reconstructs price/volume factor history against *today's*
Nifty500 constituents and sector taxonomy (MASTER_SPEC 4.5): "Backfill means
historical price/volume signals using today's constituents and taxonomy,
explicitly survivorship-biased." That single sentence drives every design
choice below:

* ``as_of`` walks real month-end sessions from the price store, never a
  fabricated fixed count of points (MASTER_SPEC 3, line ~175).
* ``members``/``groups`` are the *current* universe capture and sector
  taxonomy (survivorship bias is explicit, never hidden).
* Price reads use the run clock as their vintage, not the historical
  as_of's own cutoff: T4 (MASTER_SPEC 7.5) explicitly exempts "historical
  price vintages explicitly marked backfill/legacy" from the PIT boundary
  check, so backfill intentionally reads the latest reconciled price series
  rather than what was known back then.
* Only ``factor_registry.backfillable = 1`` rows are ever written to
  ``factor_values``; attribute/fundamental factors are computed by
  ``factors.registry.compute_all`` (it has no per-factor selection knob) and
  then dropped before anything is persisted.
* A cohort is only published once every member has the longest replayed
  factor's lookback of price history before ``as_of``; the run reports what
  it actually managed, never a promised horizon.
* ``prices_monthly.mcap_inr``/``shares_out`` are explicitly nulled out for
  every backfill cohort before publishing: MASTER_SPEC 4.5 says "Attributes
  such as current market cap are not backfillable price factors," and
  ``monthly_panel`` would otherwise resolve them against the run-clock
  ``knowledge_cutoff`` used for backfill price reads -- silently stamping
  today's market cap onto a historical cohort. T4's PIT exemption (7.5) is
  scoped to price vintages, not attribute snapshots, so this is not
  optional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pandas as pd

from quant.data.prices import PriceStore, monthly_panel
from quant.db.core import append_rows, canonical_json, sha256_text
from quant.evaluation.evaluate import run as evaluate_run
from quant.evaluation.labels import mature
from quant.factors import registry as factor_registry
from quant.run import knowledge_cutoff as historical_knowledge_cutoff
from quant.sectors import taxonomy
from quant.types import Draft, Result

if TYPE_CHECKING:
    from quant.run import RunContext

SURVIVORSHIP_CAVEAT = (
    "Backfill track uses historical price/volume series only and does not "
    "reflect point-in-time fundamental statements or survivorship bias. "
    "A negative backfill result is a valid research outcome."
)


def _run_clock_iso(ctx: "RunContext", fallback: str) -> str:
    if ctx is not None and getattr(ctx, "clock", None) is not None:
        return ctx.clock.iso()
    return fallback


def _price_store_securities(store: PriceStore) -> list[int]:
    with store.conn() as p_conn:
        rows = p_conn.execute("SELECT DISTINCT security_id FROM prices_daily ORDER BY security_id").fetchall()
    return [int(r[0]) for r in rows]


def _members_from_price_store(conn, store: PriceStore) -> pd.DataFrame:
    sids_all = _price_store_securities(store)
    if not sids_all:
        return pd.DataFrame(columns=["security_id", "isin", "symbol", "company_name", "nse_sector", "series"])
    placeholders = ",".join("?" for _ in sids_all)
    sec_rows = conn.execute(
        f"SELECT security_id, isin, name FROM securities WHERE security_id IN ({placeholders})",
        sids_all,
    ).fetchall()
    sec_map = {int(r[0]): (r[1], r[2]) for r in sec_rows}
    return pd.DataFrame(
        [
            {
                "security_id": sid,
                "isin": sec_map.get(sid, ("", ""))[0],
                "symbol": "",
                "company_name": sec_map.get(sid, ("", ""))[1],
                "nse_sector": "UNCLASSIFIED",
                "series": "EQ",
            }
            for sid in sids_all
        ]
    )


def _resolve_members(
    conn, store: PriceStore, run_clock_date: str
) -> tuple[pd.DataFrame, pd.Series, str, list[str]]:
    """Today's Nifty500 membership and sector groups (survivorship-biased).

    Returns (members_df, groups, membership_source, dq_notes).
    """
    dq_notes: list[str] = []
    cap_row = conn.execute(
        "SELECT MAX(observed_at) FROM universe_membership WHERE index_name = 'NIFTY500'"
    ).fetchone()
    latest_observed_at = cap_row[0] if cap_row else None

    if latest_observed_at:
        members_df = pd.read_sql_query(
            """
            SELECT m.security_id, s.isin, m.nse_symbol as symbol, s.name as company_name,
                   COALESCE(m.nse_sector, 'UNCLASSIFIED') as nse_sector,
                   COALESCE(m.series, 'EQ') as series
            FROM universe_membership m
            JOIN securities s ON s.security_id = m.security_id
            WHERE m.index_name = 'NIFTY500' AND m.observed_at = ?
            ORDER BY m.security_id ASC
            """,
            conn,
            params=(latest_observed_at,),
        )
        membership_source = "universe_membership"
    else:
        members_df = _members_from_price_store(conn, store)
        membership_source = "price_store_fallback"
        dq_notes.append(
            "No universe_membership capture found for NIFTY500; backfill members were drawn "
            "directly from securities present in the price store and sector groups were set "
            "to UNCLASSIFIED (no taxonomy lookup performed)."
        )

    sids_members = sorted(int(x) for x in members_df["security_id"].unique()) if not members_df.empty else []

    if membership_source == "universe_membership" and sids_members:
        groups = taxonomy.groups_at(conn, run_clock_date, sids_members)
    else:
        groups = pd.Series({sid: "UNCLASSIFIED" for sid in sids_members}, name="sector_group", dtype=object)

    return members_df, groups, membership_source, dq_notes


def _backfillable_factors(conn) -> tuple[set[str], int]:
    rows = conn.execute(
        "SELECT factor_id, lookback_days FROM factor_registry "
        "WHERE backfillable = 1 AND status IN ('active', 'shadow', 'probation')"
    ).fetchall()
    ids = {r[0] for r in rows}
    max_lookback = max((int(r[1]) for r in rows), default=0)
    return ids, max_lookback


def _month_end_candidates(store: PriceStore, start: str, end: str) -> list[str]:
    sessions = store.session_dates(start, end, min_securities=1)
    month_ends: dict[str, str] = {}
    for d in sessions:
        key = d[:7]
        if key not in month_ends or d > month_ends[key]:
            month_ends[key] = d
    return sorted(month_ends.values())


def _warmup_filter(
    store: PriceStore, sids: list[int], candidates: list[str], max_lookback: int
) -> tuple[list[str], list[str]]:
    """Split candidates into (ready, excluded) using session counts <= as_of."""
    if not sids or not candidates:
        return [], list(candidates)
    if max_lookback <= 0:
        return list(candidates), []

    ready: list[str] = []
    excluded: list[str] = []
    placeholders = ",".join("?" for _ in sids)
    with store.conn() as p_conn:
        for as_of in candidates:
            rows = p_conn.execute(
                f"SELECT security_id, COUNT(*) FROM prices_daily "
                f"WHERE security_id IN ({placeholders}) AND date <= ? GROUP BY security_id",
                [*sids, as_of],
            ).fetchall()
            counts = {int(r[0]): int(r[1]) for r in rows}
            min_count = min((counts.get(sid, 0) for sid in sids), default=0)
            if min_count >= max_lookback:
                ready.append(as_of)
            else:
                excluded.append(as_of)
    return ready, excluded


def _definition_hash(conn, backfillable_ids: set[str], cfg: Any) -> str:
    if not backfillable_ids:
        rows = []
    else:
        placeholders = ",".join("?" for _ in backfillable_ids)
        rows = conn.execute(
            f"SELECT factor_id, version, code_sha256 FROM factor_registry WHERE factor_id IN ({placeholders})",
            list(backfillable_ids),
        ).fetchall()
    payload = {
        "track": "backfill",
        "backfillable_factors": sorted(tuple(r) for r in rows),
        "policy_sha256": getattr(cfg, "policy_sha256", ""),
    }
    return sha256_text(canonical_json(payload))


def _membership_hash(members_df: pd.DataFrame) -> str:
    if members_df.empty:
        return sha256_text(canonical_json([]))
    cols = [c for c in ("security_id", "isin", "symbol", "series") if c in members_df.columns]
    rows = members_df.sort_values("security_id")[cols].astype(str).values.tolist()
    return sha256_text(canonical_json(rows))


def replay(ctx: "RunContext", start: str, end: str) -> Result:
    """Replay historical price/volume factors and evaluate the backfill track.

    Backfill cohorts use today's Nifty500 membership and sector taxonomy
    (survivorship-biased by design) but replay real, PIT price/volume
    factor computations through ``quant.factors.registry.compute_all``,
    keeping only ``backfillable = 1`` factor rows. No factor value is ever
    fabricated from a price level or a hard-coded sector.
    """
    conn = ctx.conn
    if conn is None:
        return Result(status="ok", counts={"cohorts": 0})

    run_id = getattr(ctx, "run_id", None) or 1
    run_clock_iso = _run_clock_iso(ctx, f"{end}T23:59:59.999999Z")
    run_clock_date = run_clock_iso[:10]

    store: PriceStore = getattr(ctx, "store", None) or PriceStore(ctx.cfg.paths.prices_db, state_conn=conn)

    members_df, groups, membership_source, dq_notes = _resolve_members(conn, store, run_clock_date)
    sids_members = sorted(int(x) for x in members_df["security_id"].unique()) if not members_df.empty else []

    backfillable_ids, max_lookback = _backfillable_factors(conn)
    candidate_as_of = _month_end_candidates(store, start, end)

    if not sids_members:
        ready_as_of: list[str] = []
        warmup_excluded: list[str] = list(candidate_as_of)
    else:
        ready_as_of, warmup_excluded = _warmup_filter(store, sids_members, candidate_as_of, max_lookback)

    def_hash = _definition_hash(conn, backfillable_ids, ctx.cfg) if sids_members else ""
    mem_hash = _membership_hash(members_df)

    cohorts_created = 0
    factor_rows_inserted = 0
    panel_rows_inserted = 0

    for as_of in ready_as_of:
        cohort_id = f"backfill:{as_of}"
        hist_cutoff = historical_knowledge_cutoff(as_of)
        source_refs = {
            "track": "backfill",
            "survivorship_biased": True,
            "membership_source": membership_source,
            "vintage_at": run_clock_iso,
            "historical_knowledge_cutoff": hist_cutoff,
        }

        # Draft used only for computation: knowledge_cutoff is the run clock so
        # that (a) currently-registered factors are eligible regardless of
        # when this historical as_of falls, and (b) FactorInputs price reads
        # use the run clock as their vintage (T4's backfill/legacy exemption).
        compute_draft = Draft(
            cohort_id=cohort_id,
            as_of=as_of,
            track="backfill",
            knowledge_cutoff=run_clock_iso,
            definition_hash=def_hash,
            members=members_df,
            groups=groups,
            source_refs=source_refs,
            membership_hash=mem_hash,
        )

        computed = factor_registry.compute_all(ctx, compute_draft)
        if computed is None or computed.empty:
            continue
        keep = computed[computed["factor_id"].isin(backfillable_ids)].copy()
        if keep.empty:
            continue

        now_iso = run_clock_iso
        cohort_row = pd.DataFrame(
            [
                {
                    "cohort_id": cohort_id,
                    "as_of": as_of,
                    "track": "backfill",
                    "knowledge_cutoff": hist_cutoff,
                    "definition_hash": def_hash,
                    "membership_hash": mem_hash,
                    "source_refs_json": canonical_json(source_refs),
                    "published_at": now_iso,
                    "generated_at": now_iso,
                    "is_clean": 0,
                    "run_id": run_id,
                }
            ]
        )
        inserted_cohort = append_rows(ctx, "cohorts", cohort_row, ["cohort_id"])
        cohorts_created += inserted_cohort

        panel = monthly_panel(ctx, compute_draft)
        if not panel.empty:
            # MASTER_SPEC 4.5: "Attributes such as current market cap are not
            # backfillable price factors." ``monthly_panel`` resolves
            # mcap_inr/shares_out from ``security_attributes`` filtered by
            # ``captured_at <= draft.knowledge_cutoff`` -- and
            # ``compute_draft.knowledge_cutoff`` is deliberately the run
            # clock (see the module docstring), not this historical
            # ``as_of``'s own cutoff. Left alone that would persist TODAY's
            # market-cap/shares-outstanding snapshot onto a cohort dated
            # months or years earlier. T4 (MASTER_SPEC 7.5)'s PIT exemption
            # is scoped to "historical price vintages" (close/volume/
            # dividend/split series feeding TRI etc.); it does not extend to
            # ``security_attributes`` snapshots, so there is no basis for
            # exempting mcap/shares_out here. Null them out rather than
            # reading a second, historically-cut draft, so the price fields
            # (close_raw/tri/adv_63_inr) keep using the same run-clock
            # vintage as every other backfill price read.
            if "mcap_inr" in panel.columns:
                panel["mcap_inr"] = float("nan")
            if "shares_out" in panel.columns:
                panel["shares_out"] = float("nan")
            panel_rows_inserted += append_rows(ctx, "prices_monthly", panel, ["cohort_id", "security_id"])

        factor_rows_inserted += append_rows(ctx, "factor_values", keep, ["cohort_id", "security_id", "factor_id"])

    # Mature and evaluate whatever backfill cohorts exist in range, including
    # ones seeded outside this run.
    mature(ctx, through=end)
    eval_res = evaluate_run(ctx, through=end, track="backfill")

    actual_cohorts = conn.execute(
        "SELECT COUNT(*) FROM cohorts WHERE track = 'backfill' AND as_of >= ? AND as_of <= ?",
        (start, end),
    ).fetchone()[0]

    details: dict[str, Any] = {
        "requested_start": start,
        "requested_end": end,
        "candidate_month_ends": candidate_as_of,
        "warmup_excluded_as_of": warmup_excluded,
        "cohorts_created": cohorts_created,
        "actual_cohorts": actual_cohorts,
        "factor_rows_inserted": factor_rows_inserted,
        "panel_rows_inserted": panel_rows_inserted,
        "membership_source": membership_source,
        "survivorship_caveat": SURVIVORSHIP_CAVEAT,
        "negative_results_accepted": True,
        "evaluations_inserted": eval_res.counts.get("inserted", 0),
    }
    if dq_notes:
        details["dq_notes"] = dq_notes

    return Result(
        status="ok",
        counts={"cohorts": actual_cohorts, "cohorts_created": cohorts_created},
        details=details,
    )
