"""Sector taxonomy, small-sector merges and point-in-time sector groups."""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, List, Optional
import pandas as pd

from quant.config import Config
from quant.run import RunContext
from quant.types import Result


DEFAULT_RULES_CSV = "config/sector_groups_v1.csv"


def load_rules(path: str = DEFAULT_RULES_CSV) -> pd.DataFrame:
    """Load sector grouping rules from CSV."""
    if os.path.exists(path):
        return pd.read_csv(path)
    return pd.DataFrame(columns=[
        "version", "nse_sector", "yahoo_industry_pattern", "sector_group",
        "macro_sector", "merge_into", "min_group_size", "registered_on", "decision_id"
    ])


def assign_groups(
    members: pd.DataFrame,
    rules: pd.DataFrame,
    yahoo_industry: Optional[pd.Series] = None,
    cfg: Optional[Config] = None,
) -> pd.DataFrame:
    """Assign sector groups to universe members with small-sector merge rules.
    
    Output columns: security_id, nse_sector, sector_group, macro_sector, merged_from.
    Unclassified sectors are explicitly mapped to 'UNCLASSIFIED'.
    """
    min_size = 8
    split_fin = False
    if cfg is not None and hasattr(cfg, "sectors"):
        min_size = getattr(cfg.sectors, "min_group_size", 8)
        split_fin = getattr(cfg.sectors, "split_financials", False)

    if yahoo_industry is None:
        yahoo_industry = pd.Series(dtype=str)

    # Fast rules lookup indexed by nse_sector
    base_rules: dict[str, dict[str, Any]] = {}
    for _, r in rules.iterrows():
        sec = str(r["nse_sector"]).strip()
        pattern = str(r.get("yahoo_industry_pattern", "") or "").strip()
        if not pattern or not split_fin:
            # base rule without pattern
            if sec not in base_rules:
                target_val = r.get("merge_into")
                merge_target = None if (pd.isna(target_val) or not str(target_val).strip() or str(target_val).strip() == "None") else str(target_val).strip()
                base_rules[sec] = {
                    "sector_group": str(r["sector_group"]),
                    "macro_sector": str(r["macro_sector"]),
                    "merge_into": merge_target,
                    "min_group_size": int(r.get("min_group_size", min_size)),
                }

    records = []
    for _, row in members.iterrows():
        sid = row["security_id"]
        nse_sec = str(row.get("nse_sector", "")).strip()
        y_ind = str(yahoo_industry.get(sid, "") or "").strip()

        if split_fin and nse_sec == "Financial Services":
            # Match Yahoo industry for split
            if re.search(r"bank", y_ind, re.IGNORECASE):
                group = "FS_BANKS"
            elif re.search(r"credit|mortgage|conglomerate|insurance", y_ind, re.IGNORECASE):
                group = "FS_LENDERS"
            else:
                group = "FS_MARKETS"
            records.append({
                "security_id": sid,
                "nse_sector": nse_sec,
                "sector_group": group,
                "macro_sector": "Financial Services",
                "merge_into": None,
                "merged_from": None,
                "min_group_size": min_size,
            })
        elif nse_sec in base_rules:
            rule = base_rules[nse_sec]
            records.append({
                "security_id": sid,
                "nse_sector": nse_sec,
                "sector_group": rule["sector_group"],
                "macro_sector": rule["macro_sector"],
                "merge_into": rule["merge_into"],
                "merged_from": None,
                "min_group_size": rule["min_group_size"],
            })
        else:
            records.append({
                "security_id": sid,
                "nse_sector": nse_sec,
                "sector_group": "UNCLASSIFIED",
                "macro_sector": "UNCLASSIFIED",
                "merge_into": None,
                "merged_from": None,
                "min_group_size": min_size,
            })

    assigned = pd.DataFrame(records)
    if assigned.empty:
        return pd.DataFrame(columns=["security_id", "nse_sector", "sector_group", "macro_sector", "merged_from"])

    # 2. Count group sizes and apply merges
    # Group size is counted across members for classified groups
    counts = assigned[assigned["sector_group"] != "UNCLASSIFIED"]["sector_group"].value_counts().to_dict()

    for i, row in assigned.iterrows():
        grp = row["sector_group"]
        if grp == "UNCLASSIFIED":
            continue
        cnt = counts.get(grp, 0)
        target = row["merge_into"]
        req_min = row["min_group_size"]

        if cnt < req_min and target is not None and not pd.isna(target):
            assigned.at[i, "merged_from"] = grp
            assigned.at[i, "sector_group"] = target
            # Update macro_sector if target is in base_rules
            if target in base_rules:
                assigned.at[i, "macro_sector"] = base_rules[target]["macro_sector"]

    return assigned[["security_id", "nse_sector", "sector_group", "macro_sector", "merged_from"]]


def capture(ctx: RunContext, members: pd.DataFrame) -> Result:
    """Assign sector groups and store point-in-time sector mapping."""
    rules_path = os.path.join(ctx.cfg.paths.data_dir, "../config/sector_groups_v1.csv")
    if not os.path.exists(rules_path):
        rules_path = "config/sector_groups_v1.csv"
    rules = load_rules(rules_path)

    # Sync sector_group_def table if empty
    cur = ctx.conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sector_group_def")
    if cur.fetchone()[0] == 0 and not rules.empty:
        for _, r in rules.iterrows():
            target_val = r.get("merge_into")
            m_target = None if (pd.isna(target_val) or not str(target_val).strip() or str(target_val).strip() == "None") else str(target_val).strip()
            cur.execute(
                """
                INSERT OR IGNORE INTO sector_group_def (
                    version, nse_sector, yahoo_industry_pattern, sector_group,
                    macro_sector, merge_into, min_group_size, registered_on, decision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(r.get("version", 1)),
                    str(r["nse_sector"]),
                    str(r.get("yahoo_industry_pattern", "") or ""),
                    str(r["sector_group"]),
                    str(r["macro_sector"]),
                    m_target,
                    int(r.get("min_group_size", 8)),
                    str(r.get("registered_on", "2026-01-01")),
                    str(r.get("decision_id", "DEC_INIT")),
                )
            )

    assigned = assign_groups(members, rules, yahoo_industry=None, cfg=ctx.cfg)
    now_iso = ctx.clock.iso()
    def_ver = getattr(ctx.cfg.sectors, "group_def_version", 1)

    for _, row in assigned.iterrows():
        sid = int(row["security_id"])
        nse_sec = row["nse_sector"]
        grp = row["sector_group"]
        conf = 1.0 if grp != "UNCLASSIFIED" else 0.0

        # Close any prior open record if valid_from < ctx.as_of
        cur.execute(
            """
            UPDATE sector_map
            SET valid_to = ?
            WHERE security_id = ? AND valid_to IS NULL AND valid_from < ?
            """,
            (ctx.as_of, sid, ctx.as_of)
        )

        # Insert new point-in-time mapping (append-only: an existing row for the same
        # valid_from is kept; a changed classification needs a new valid_from)
        cur.execute(
            "SELECT sector_group FROM sector_map WHERE security_id = ? AND valid_from = ?",
            (sid, ctx.as_of),
        )
        if cur.fetchone() is not None:
            continue
        cur.execute(
            """
            INSERT INTO sector_map (
                security_id, observed_at, valid_from, valid_to,
                nse_sector, yahoo_sector, yahoo_industry,
                sector_group, group_def_version, source, confidence
            ) VALUES (?, ?, ?, NULL, ?, NULL, NULL, ?, ?, 'nse_csv', ?)
            """,
            (sid, now_iso, ctx.as_of, nse_sec, grp, def_ver, conf)
        )

    return Result(
        status="ok",
        counts={"rows": len(assigned)},
        details={"message": f"Captured {len(assigned)} sector memberships"},
    )


def groups_at(conn: sqlite3.Connection, cutoff: str, security_ids: List[int]) -> pd.Series:
    """Return point-in-time sector groups for given security IDs at cutoff date."""
    if not security_ids:
        return pd.Series(dtype=str)

    cur = conn.cursor()
    out = {}
    for sid in security_ids:
        cur.execute(
            """
            SELECT sector_group FROM sector_map
            WHERE security_id = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)
            ORDER BY valid_from DESC LIMIT 1
            """,
            (sid, cutoff, cutoff)
        )
        row = cur.fetchone()
        out[sid] = row[0] if row else "UNCLASSIFIED"

    return pd.Series(out, name="sector_group")
