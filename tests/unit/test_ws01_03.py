import pandas as pd
import pytest

from quant.config import load as load_config
from quant.sectors.taxonomy import assign_groups, capture as taxonomy_capture, groups_at
from quant.sectors.crosswalk import yahoo_to_nse


def make_test_members_and_rules():
    # 5 members in Financial Services, 3 in Textiles, 10 in Consumer Durables, 1 in UnknownSector
    members = pd.DataFrame([
        {"security_id": 1, "nse_sector": "Financial Services"},
        {"security_id": 2, "nse_sector": "Financial Services"},
        {"security_id": 3, "nse_sector": "Financial Services"},
        {"security_id": 4, "nse_sector": "Financial Services"},
        {"security_id": 5, "nse_sector": "Financial Services"},
        {"security_id": 6, "nse_sector": "Textiles"},
        {"security_id": 7, "nse_sector": "Textiles"},
        {"security_id": 8, "nse_sector": "Textiles"},
        {"security_id": 9, "nse_sector": "Consumer Durables"},
        {"security_id": 10, "nse_sector": "Consumer Durables"},
        {"security_id": 11, "nse_sector": "Consumer Durables"},
        {"security_id": 12, "nse_sector": "Consumer Durables"},
        {"security_id": 13, "nse_sector": "Consumer Durables"},
        {"security_id": 14, "nse_sector": "Consumer Durables"},
        {"security_id": 15, "nse_sector": "Consumer Durables"},
        {"security_id": 16, "nse_sector": "Consumer Durables"},
        {"security_id": 17, "nse_sector": "Consumer Durables"},
        {"security_id": 18, "nse_sector": "Consumer Durables"},
        {"security_id": 19, "nse_sector": "Mysterious Sector"},
    ])

    rules = pd.DataFrame([
        {"version": 1, "nse_sector": "Financial Services", "yahoo_industry_pattern": "", "sector_group": "Financial Services", "macro_sector": "Financial Services", "merge_into": None, "min_group_size": 8},
        {"version": 1, "nse_sector": "Consumer Durables", "yahoo_industry_pattern": "", "sector_group": "Consumer Durables", "macro_sector": "Consumer Discretionary", "merge_into": None, "min_group_size": 8},
        {"version": 1, "nse_sector": "Textiles", "yahoo_industry_pattern": "", "sector_group": "Textiles", "macro_sector": "Consumer Discretionary", "merge_into": "Consumer Durables", "min_group_size": 8},
    ])

    yahoo_ind = pd.Series(["Banks - Regional"] * len(members), index=members["security_id"])
    return members, rules, yahoo_ind


def test_assign_groups_rules_and_merges():
    cfg = load_config()
    members, rules, yahoo_ind = make_test_members_and_rules()

    assigned = assign_groups(members, rules, yahoo_ind, cfg)
    
    # Financial Services unsplit initially
    fs_rows = assigned[assigned["nse_sector"] == "Financial Services"]
    assert (fs_rows["sector_group"] == "Financial Services").all()

    # Textiles (size 3 < min_group_size 8) merged into Consumer Durables
    textile_rows = assigned[assigned["nse_sector"] == "Textiles"]
    assert (textile_rows["sector_group"] == "Consumer Durables").all()
    assert (textile_rows["merged_from"] == "Textiles").all()

    # Mysterious Sector unclassified
    unknown_rows = assigned[assigned["nse_sector"] == "Mysterious Sector"]
    assert (unknown_rows["sector_group"] == "UNCLASSIFIED").all()
    assert (unknown_rows["macro_sector"] == "UNCLASSIFIED").all()


def test_taxonomy_capture_and_groups_at(ctx):
    cfg = load_config()
    members, rules, yahoo_ind = make_test_members_and_rules()

    # Insert securities 1..19 to satisfy FK
    for sid in members["security_id"]:
        ctx.conn.execute(
            "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
            "VALUES (?, ?, 'Name', '2026-01-01', '2026-09-30', 'listed')",
            (sid, f"INE{sid:09d}")
        )

    # Capture taxonomy groups
    res = taxonomy_capture(ctx, members)
    assert res.status == "ok"

    # groups_at query
    groups = groups_at(ctx.conn, cutoff=ctx.as_of, security_ids=[6, 9, 19])
    assert groups[6] == "Consumer Durables"  # Merged from Textiles
    assert groups[9] == "Consumer Durables"
    assert groups[19] == "UNCLASSIFIED"


def test_crosswalk_mapping():
    crosswalk_df = pd.DataFrame([
        {"yahoo_sector": "Financial Services", "yahoo_industry": "Banks - Regional", "nse_sector": "Financial Services", "confidence": 1.0},
        {"yahoo_sector": "Technology", "yahoo_industry": "Software - Application", "nse_sector": "Information Technology", "confidence": 0.95},
    ])

    mapped, conf = yahoo_to_nse("Financial Services", "Banks - Regional", crosswalk_df)
    assert mapped == "Financial Services"
    assert conf == 1.0

    unmapped, conf_un = yahoo_to_nse("Nonexistent", "Unknown", crosswalk_df)
    assert unmapped == "UNCLASSIFIED"
    assert conf_un == 0.0


def test_financial_services_split_when_enabled():
    cfg = load_config()
    cfg.sectors.split_financials = True

    members = pd.DataFrame([
        {"security_id": 1, "nse_sector": "Financial Services"},
        {"security_id": 2, "nse_sector": "Financial Services"},
        {"security_id": 3, "nse_sector": "Financial Services"},
    ])
    rules = pd.DataFrame([
        {"version": 1, "nse_sector": "Financial Services", "yahoo_industry_pattern": "", "sector_group": "Financial Services", "macro_sector": "Financial Services", "merge_into": None, "min_group_size": 1},
    ])
    yahoo_ind = pd.Series({
        1: "Banks - Regional",
        2: "Credit Services",
        3: "Capital Markets",
    })

    assigned = assign_groups(members, rules, yahoo_ind, cfg)
    assert assigned.loc[assigned["security_id"] == 1, "sector_group"].values[0] == "FS_BANKS"
    assert assigned.loc[assigned["security_id"] == 2, "sector_group"].values[0] == "FS_LENDERS"
    assert assigned.loc[assigned["security_id"] == 3, "sector_group"].values[0] == "FS_MARKETS"


def test_future_reclassification_does_not_change_past_cohort_pit(ctx):
    # Insert security
    ctx.conn.execute(
        "INSERT OR IGNORE INTO securities (security_id, isin, name, first_seen, last_seen, status) "
        "VALUES (101, 'INE101A01001', 'Test PIT', '2026-01-01', '2026-09-30', 'listed')"
    )

    # Initial capture at T1: 2026-08-31
    ctx.as_of = "2026-08-31"
    members_t1 = pd.DataFrame([{"security_id": 101, "nse_sector": "Financial Services"}])
    res1 = taxonomy_capture(ctx, members_t1)
    assert res1.status == "ok"

    # Query at T1
    g_t1 = groups_at(ctx.conn, cutoff="2026-08-31", security_ids=[101])
    assert g_t1[101] == "Financial Services"

    # Reclassification capture at T2: 2026-09-30 (e.g. company changed business/sector to Healthcare)
    ctx.as_of = "2026-09-30"
    members_t2 = pd.DataFrame([{"security_id": 101, "nse_sector": "Healthcare"}])
    res2 = taxonomy_capture(ctx, members_t2)
    assert res2.status == "ok"

    # Crucial PIT invariant: Query at cutoff T1 still yields Financial Services!
    g_past = groups_at(ctx.conn, cutoff="2026-08-31", security_ids=[101])
    assert g_past[101] == "Financial Services"

    # Query at T2 yields Healthcare
    g_curr = groups_at(ctx.conn, cutoff="2026-09-30", security_ids=[101])
    assert g_curr[101] == "Healthcare"


def test_large_sector_does_not_merge():
    cfg = load_config()
    # 8 members in Textiles (>= min_group_size 8)
    members = pd.DataFrame([
        {"security_id": i, "nse_sector": "Textiles"} for i in range(1, 9)
    ])
    rules = pd.DataFrame([
        {"version": 1, "nse_sector": "Textiles", "yahoo_industry_pattern": "", "sector_group": "Textiles", "macro_sector": "Consumer Discretionary", "merge_into": "Consumer Durables", "min_group_size": 8},
    ])
    yahoo_ind = pd.Series(["Apparel Manufacturing"] * 8, index=range(1, 9))

    assigned = assign_groups(members, rules, yahoo_ind, cfg)
    assert (assigned["sector_group"] == "Textiles").all()
    assert (assigned["merged_from"].isna() | (assigned["merged_from"] == None)).all()

