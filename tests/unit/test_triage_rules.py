"""Evidence triage WP3: scan, features, rules and pack.

Rules are tested as pure functions over ``Features`` and ``evidence.EvidenceItem`` objects, one
positive and one negative test each. The recorded-row tests replay the 36 labelled anomalies (price
features only) through the same classifier. The pack and scan tests use a disposable state database
and price database under tmp_path, a FrozenClock and no network. The cohort is as_of 2026-10-30
(Friday); its knowledge cutoff is 2026-10-30T18:29:59.999999Z.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from collections import Counter
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant.config import load as load_config
from quant.data import actions
from quant.data.prices import PriceStore
from quant.errors import Refused
from quant.db.core import apply_schema, connect
from quant.run import RunContext
from quant.triage import evidence, features as feat, pack, rules, scan, store as triage_store, validate
from quant.triage.types import check_schema
from quant.types import Actor, FrozenClock

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "triage" / "rules"
AS_OF = "2026-10-30"                       # Friday
CUTOFF = "2026-10-30T18:29:59.999999Z"
NOON = "2026-10-30T06:30:00.000000Z"
ISIN = "INE000000001"
EX = "2026-10-28"                          # Wednesday
ARCHIVE_TS = "2026-10-29T10:00:00.000000Z"


@pytest.fixture(autouse=True)
def _fast_run_context(monkeypatch):
    """Every RunContext hashes the whole package and shells out to git; neither matters here (amendment F7)."""
    import quant.db.core as dbcore
    monkeypatch.setattr(dbcore, "code_sha256", lambda root=None: "0" * 64)
    monkeypatch.setattr(RunContext, "_get_git_sha", staticmethod(lambda: "test"))


# ------------------------------------------------------------------ pure-rule helpers

def F(**over) -> feat.Features:
    """A quiet EQ jump: nothing known beyond the store basics, no later bars (day 0)."""
    base = dict(isin=ISIN, ex_date=EX, as_of=AS_OF, symbol="TESTCO", security_id=1, gross_factor=0.6,
                series="EQ", placeholder=False, vendor_split_rows=(), cross_section_count=1,
                market_median_move_pct=0.3, sector_peer_median_move_pct=0.2, n_peers=10, n_post_bars=0)
    base.update(over)
    return feat.Features(**base)


def env(**over) -> dict:
    base = dict(
        schema="triage.envelope/1", provenance="human_attach", subject={"isin": ISIN, "ex_date": EX},
        url=None, title="Company notice", headline=None, published_at="2026-10-27", published_at_basis="human_declared",
        published_at_precision="date", source_class="company", relevance="entity", relevance_basis="token_match",
        excerpt="Company notice", typed_fields={}, body_sha256=None, entity_match=True, source_status="ok")
    base.update(over)
    return base


_SEQ = iter(range(1, 10_000))


def item(decisive: bool = True, **over) -> evidence.EvidenceItem:
    n = next(_SEQ)
    return evidence.EvidenceItem(f"TD-{n:016x}", f"{n:064x}", ARCHIVE_TS, env(**over), decisive, "ok" if decisive else "ok_undated")


def spos_item(prev_close=660.75, spos=400.0, **over) -> evidence.EvidenceItem:
    tf = {"prev_close": prev_close, "spos_price": spos}
    tf.update(over.pop("typed_fields", {}))
    base = dict(title="Special pre-open session for the scheme", excerpt="SYNTHETIC circular excerpt",
                source_class="exchange", typed_fields=tf)
    base.update(over)
    return item(**base)


def demerger_item(**over) -> evidence.EvidenceItem:
    base = dict(title="Board approves demerger scheme", excerpt="Board approves demerger scheme of arrangement",
                published_at="2026-09-15")
    base.update(over)
    return item(**base)


def sector_doc(**over) -> evidence.EvidenceItem:
    base = dict(title="Regulator consultation on distribution", excerpt="SYNTHETIC excerpt about distribution commissions",
                source_class="regulator", relevance="sector", entity_match=None, published_at="2026-10-27")
    base.update(over)
    return item(**base)


def run(f, *items, rejected=(), archive_dir=None):
    return rules.classify(f, list(items), rejected=rejected, archive_dir=archive_dir)


def assert_unknown(d):
    assert d.label == "unknown" and d.rule_id is None and d.action == "none" and d.confidence == "low"


# ------------------------------------------------------------------ constants and drift guards

def test_rules_version_and_engine_name():
    assert rules.RULES_VERSION == "triage-rules/1"
    assert rules.ENGINE_NAME == "rules:triage-rules/1"
    assert rules.RULE_ORDER == ("R1", "R2", "R3", "R4", "R7", "R8", "R5", "R6")


def test_engine_constants_equal_the_validators():
    """The validator re-derives R3, R4 and R7 from the same numbers; the two files must not drift."""
    assert rules.R3_WINDOW_DAYS == validate.R3_WINDOW_DAYS
    assert rules.R3_GROSS_RANGE == validate.R3_GROSS_RANGE
    assert rules.R3_PEER_MARKET_GAP_PCT == validate.R3_PEER_MARKET_GAP_PCT
    assert rules.R4_RATIO_TOLERANCE == validate.R4_RATIO_TOLERANCE
    assert rules.R7_WINDOW_DAYS == validate.R7_WINDOW_DAYS
    assert feat.flat.__kwdefaults__["product_range"] == rules.R6_PRODUCT_RANGE


# ------------------------------------------------------------------ R1 placeholder / series

def test_r1_placeholder_waits_and_grades_by_primary_document():
    d = run(F(placeholder=True, gross_factor=0.374))
    assert (d.label, d.rule_id, d.action, d.confidence, d.subtype) == ("pending_listing", "R1", "wait", "medium", "index_placeholder")
    assert d.recheck_on == "2026-11-06"                       # as_of + 7 days
    assert d.factor is None and d.resolve_kind is None
    high = run(F(placeholder=True), item(source_class="exchange", title="New listing notice"))
    assert high.confidence == "high" and high.cited


def test_r1_fires_on_a_placeholder_anomaly():
    d = run(feat.Features(isin=ISIN, ex_date=EX, as_of=AS_OF, anomaly_kind="index_placeholder", placeholder=True))
    assert (d.label, d.rule_id, d.action) == ("pending_listing", "R1", "wait")


def test_r1_non_eq_series_is_a_pending_listing_wait_as_the_design_says():
    """Design 5.2 row 1: a series other than EQ at the jump is R1, ``pending_listing``, action wait, medium."""
    d = run(F(series="BE"))
    assert (d.label, d.rule_id, d.subtype, d.action, d.confidence) == ("pending_listing", "R1", "series_not_eq", "wait", "medium")
    assert d.recheck_on == "2026-11-06" and d.factor is None and d.resolve_kind is None
    assert "series BE" in d.claims[0][1]
    # table 5.3: R1 plus an attached primary document is high
    assert run(F(series="BE"), item(source_class="exchange", title="Series change notice")).confidence == "high"


def test_r1_placeholder_subtype_wins_over_a_non_eq_series():
    d = run(F(series="BE", placeholder=True))
    assert (d.label, d.subtype) == ("pending_listing", "index_placeholder")


def test_r1_series_half_runs_first_so_later_rules_do_not_see_a_non_eq_series():
    """Order 1 in the design: R1 beats R2, R3 and R8 for a non-EQ series (round 2 owner-ruling finding F1)."""
    assert run(F(series="BE", gross_factor=0.6), spos_item()).rule_id == "R1"
    f = F(series="BE", gross_factor=0.40, sector_peer_median_move_pct=0.5, market_median_move_pct=0.3)
    assert run(f, demerger_item()).rule_id == "R1"
    assert run(F(series="BE", gross_factor=0.9, market_median_move_pct=-5.0)).rule_id == "R1"
    assert rules.RULE_ORDER[0] == "R1" and not hasattr(rules, "SERIES_BRANCH_AFTER")


def test_r1_does_not_fire_without_a_placeholder_and_unknown_is_not_false():
    assert run(F(placeholder=False, series="EQ")).rule_id != "R1"
    assert run(F(placeholder=None, series=None)).rule_id != "R1"      # unknown series and placeholder: no call


def test_r1_ignores_an_undated_or_agent_primary_document_for_high_confidence():
    d = run(F(placeholder=True), item(decisive=False, source_class="exchange", published_at=None, published_at_basis=None,
                                       published_at_precision=None),
            item(source_class="exchange", provenance="agent_attach", published_at_basis="agent_declared"))
    assert d.confidence == "medium"


# ------------------------------------------------------------------ R2 exchange SPOS

def test_r2_spos_factor_and_high_confidence_needs_the_body_file(tmp_path):
    body = b"synthetic circular body"
    sha = evidence.store_body(tmp_path, body)
    f = F(gross_factor=0.598487, market_median_move_pct=-0.92, sector_peer_median_move_pct=-0.37)
    d = run(f, spos_item(body_sha256=sha), archive_dir=tmp_path)
    assert (d.label, d.rule_id, d.action, d.resolve_kind, d.factor_basis) == ("demerger", "R2", "resolve", "demerger", "exchange_spos")
    assert d.factor == pytest.approx(660.75 / 400.0, abs=1e-6) and d.confidence == "high"
    assert [n for n, _, _ in d.factor_inputs] == ["prev_close", "spos_price"]
    assert d.needs_force is False
    assert "+1.2%" in d.alternatives[0][1] and "1.6709" in d.alternatives[0][1]      # 1/gross with its gap to the exchange figure
    no_body_dir = run(f, spos_item(body_sha256=sha))                                  # no archive dir: body unverifiable
    url_only = run(f, spos_item(), archive_dir=tmp_path)
    missing_file = run(f, spos_item(body_sha256="0" * 64), archive_dir=tmp_path)
    assert no_body_dir.confidence == url_only.confidence == missing_file.confidence == "medium"


def test_r2_high_confidence_uses_the_agreeing_candidate_whose_body_verifies(tmp_path):
    sha = evidence.store_body(tmp_path, b"second circular body")
    broken = spos_item(body_sha256="0" * 64, title="First circular, body file lost")
    good = spos_item(body_sha256=sha, title="Second circular")
    for order in ((broken, good), (good, broken)):
        d = run(F(gross_factor=0.598487), *order, archive_dir=tmp_path)
        assert d.rule_id == "R2" and d.confidence == "high" and d.cited == (good.capture_id,)
    assert run(F(gross_factor=0.598487), broken, archive_dir=tmp_path).confidence == "medium"


def test_r2_takes_the_scheme_and_rights_action_types():
    assert run(F(), spos_item(typed_fields={"action_type": "scheme"})).label == "scheme"
    d = run(F(), spos_item(typed_fields={"action_type": "rights"}))
    assert (d.label, d.resolve_kind) == ("rights", "rights")


def test_r2_does_not_fire_without_a_verified_primary_dated_entity_document():
    for bad in (
        spos_item(source_class="news"),
        spos_item(provenance="agent_attach", published_at_basis="agent_declared"),
        spos_item(entity_match=None),
        spos_item(entity_match=False),
        spos_item(relevance="sector"),
        spos_item(decisive=False, published_at=None, published_at_basis=None, published_at_precision=None),
        item(source_class="exchange", typed_fields={"prev_close": 660.75}),
        spos_item(spos=0.0),
    ):
        assert run(F(), bad).rule_id != "R2"


def test_r2_bad_inputs_and_conflicts_are_reported_not_acted_on():
    d = run(F(), spos_item(prev_close=300.0, spos=400.0))
    assert d.rule_id != "R2" and any("not above 1" in n for n in d.needs_human)
    d = run(F(), spos_item(typed_fields={"action_type": "bonus"}))
    assert d.rule_id != "R2" and any("action_type" in n for n in d.needs_human)
    conflict = run(F(), spos_item(), spos_item(spos=380.0))
    assert_unknown(conflict)
    assert any("conflicting SPOS" in n for n in conflict.needs_human)
    agree = run(F(), spos_item(), spos_item(prev_close=660.75, spos=400.0, title="Second circular"))
    assert agree.rule_id == "R2"


def test_r2_out_of_band_factor_needs_force():
    d = run(F(gross_factor=0.9), spos_item())          # 0.9 * 1.65 is far outside the jump band
    assert d.rule_id == "R2" and d.needs_force is True


# ------------------------------------------------------------------ R3 demerger document

def r3_features(**over):
    base = dict(gross_factor=0.40, sector_peer_median_move_pct=0.5, market_median_move_pct=0.3)
    base.update(over)
    return F(**base)


def test_r3_demerger_document_gives_market_implied_factor_at_medium():
    d = run(r3_features(), demerger_item())
    assert (d.label, d.rule_id, d.confidence, d.factor_basis) == ("demerger", "R3", "medium", "market_implied")
    assert d.factor == pytest.approx(2.5) and d.factor_uncertainty_pct == 12.0 and d.needs_force is False


@pytest.mark.parametrize("features_over, doc_over", [
    ({"sector_peer_median_move_pct": 3.6}, {}),                               # peers and market differ by 3.3
    ({"vendor_split_rows": ({"date": "2026-10-27", "ratio": 2.5, "source": "price_store"},)}, {}),
    ({"vendor_split_rows": None}, {}),                                        # unknown is not "no split"
    ({"sector_peer_median_move_pct": None}, {}),
    ({"gross_factor": 0.95}, {}),
    ({"gross_factor": 0.1}, {}),
    ({}, {"published_at": "2026-05-01"}),                                     # older than 120 days
    ({}, {"source_class": "news"}),
    ({}, {"entity_match": None}),
    ({}, {"relevance": "sector"}),
    ({}, {"title": "Board approves dividend", "excerpt": "Board approves dividend"}),
])
def test_r3_negative_cases(features_over, doc_over):
    assert run(r3_features(**features_over), demerger_item(**doc_over)).rule_id != "R3"


def test_r2_wins_over_r3_when_both_documents_are_present():
    assert run(r3_features(), demerger_item(), spos_item()).rule_id == "R2"


# ------------------------------------------------------------------ R4 vendor split

VENDOR_ROW = {"date": "2026-10-26", "ratio": 1.5, "source": "price_store"}


def test_r4_vendor_split_on_another_date_is_a_misdated_adjustment():
    d = run(F(gross_factor=0.6695, vendor_split_rows=(VENDOR_ROW,)))
    assert (d.label, d.subtype, d.rule_id, d.confidence) == ("vendor_error_other", "misdated_adjustment", "R4", "medium")
    assert (d.action, d.resolve_kind, d.factor, d.factor_basis, d.needs_primary) == ("resolve", "split", 1.5, "vendor_ratio", True)
    assert d.factor_inputs == (("ratio", 1.5, "f:vendor_split_ratio"),)


def test_r4_negative_cases():
    same_day = {**VENDOR_ROW, "date": EX}
    wrong_ratio = {**VENDOR_ROW, "ratio": 2.0}
    for rows in ((), (same_day,), (wrong_ratio,), None):
        assert run(F(gross_factor=0.6695, vendor_split_rows=rows)).rule_id != "R4"


def test_r4_reads_yahoo_actions_envelopes_and_ignores_malformed_ones():
    good = item(provenance="yahoo_actions", source_class="other", relevance="context", published_at=None,
                published_at_basis=None, published_at_precision=None, decisive=False,
                typed_fields={"ticker": "TESTCO.NS", "window_days": 5, "n_splits": 1, "split_1_date": "2026-10-26",
                              "split_1_ratio": 1.5})
    d = run(F(gross_factor=0.6695, vendor_split_rows=None), good)
    assert d.rule_id == "R4" and d.cited == (good.capture_id,)
    assert d.factor_inputs == (("ratio", 1.5, good.capture_id),)
    renamed = item(provenance="yahoo_actions", source_class="other", relevance="context", published_at=None,
                   published_at_basis=None, published_at_precision=None, decisive=False,
                   typed_fields={"n_splits": 1, "split_1_when": "2026-10-26", "split_1_ratio": 1.5, "split_2_date": "2026-10-26"})
    d = run(F(gross_factor=0.6695, vendor_split_rows=None), renamed)
    assert d.rule_id != "R4" and feat.merged_vendor_rows(F(vendor_split_rows=None), [renamed]) is None
    far = item(provenance="yahoo_actions", source_class="other", relevance="context", published_at=None, published_at_basis=None,
               published_at_precision=None, decisive=False, typed_fields={"n_splits": 1, "split_1_date": "2026-10-10", "split_1_ratio": 1.5})
    assert feat.yahoo_split_rows([far], EX) == []                        # outside the five-day vendor window
    two = item(provenance="yahoo_actions", source_class="other", relevance="context", published_at=None, published_at_basis=None,
               published_at_precision=None, decisive=False,
               typed_fields={"n_splits": 2, "split_1_date": "2026-10-26", "split_1_ratio": 1.5, "split_2_date": "2026-10-29",
                             "split_2_ratio": 2.0})
    assert [(r["date"], r["ratio"]) for r in feat.yahoo_split_rows([two], EX)] == [("2026-10-26", 1.5), ("2026-10-29", 2.0)]


# ------------------------------------------------------------------ R7 genuine-event document

def r7_features(**over):
    base = dict(gross_factor=0.6415, n_peers_moved=3, market_median_move_pct=-0.4, sector_peer_median_move_pct=-4.5)
    base.update(over)
    return F(**base)


def test_r7_genuine_document_grades_by_table_5_3():
    ps = run(r7_features(), sector_doc())
    assert (ps.label, ps.rule_id, ps.confidence, ps.action, ps.resolve_kind, ps.factor) == \
        ("genuine_event", "R7", "medium", "resolve", "genuine", 1.0)
    assert all(c["result"] == "none_found" for c in ps.negative_checks)
    assert run(r7_features(n_peers_moved=1), sector_doc()).confidence == "low"               # Ps alone
    assert run(r7_features(n_peers_moved=None), sector_doc()).confidence == "low"            # unknown peers are not C2
    p_doc = item(source_class="regulator", relevance="entity", entity_match=True, published_at="2026-10-28")
    assert run(r7_features(n_peers_moved=1), p_doc).confidence == "medium"                   # P alone
    assert run(r7_features(), p_doc).confidence == "high"                                    # P and C2
    news = item(source_class="news", relevance="sector", entity_match=None, published_at="2026-10-28")
    assert run(r7_features(), news).confidence == "medium"                                   # N and C2
    assert run(r7_features(n_peers_moved=0), news).confidence == "low"


@pytest.mark.parametrize("doc_over", [
    {"published_at": "2026-10-21"},                  # 7 days before the ex-date
    {"published_at": "2026-10-29"},                  # after the ex-date
    {"relevance": "context"},
    {"relevance": "entity", "entity_match": False},
    {"relevance": "entity", "entity_match": None},
])
def test_r7_negative_cases_on_the_document(doc_over):
    assert run(r7_features(), sector_doc(**doc_over)).rule_id != "R7"


def test_r7_window_edges_are_inclusive():
    assert run(r7_features(), sector_doc(published_at="2026-10-23")).rule_id == "R7"
    assert run(r7_features(), sector_doc(published_at=EX)).rule_id == "R7"


def test_r7_undated_document_cannot_decide():
    undated = sector_doc(decisive=False, published_at=None, published_at_basis=None, published_at_precision=None)
    assert run(r7_features(), undated).rule_id != "R7"


@pytest.mark.parametrize("features_over", [
    {"placeholder": True},                             # R1 fires first, never R7
    {"placeholder": None},                             # unknown placeholder blocks the negative check
    {"series": None},
    {"vendor_split_rows": None},
    {"vendor_split_rows": ({"date": "2026-10-20", "ratio": 2.0, "source": "price_store"},)},
])
def test_r7_needs_every_negative_check_known_and_clean(features_over):
    assert run(r7_features(**features_over), sector_doc()).rule_id != "R7"


def test_r7_is_blocked_by_a_demerger_keyword_in_any_attached_document():
    d = run(r7_features(), sector_doc(), item(decisive=False, title="Scheme of arrangement notice", excerpt=None,
                                                   published_at=None, published_at_basis=None, published_at_precision=None))
    assert d.rule_id != "R7" and d.label == "unknown"
    assert d.missing_evidence[0].startswith("resolve the counter-evidence")


def test_r7_unreadable_capture_makes_the_keyword_check_unknown():
    d = run(r7_features(), sector_doc(), rejected=[("TD-" + "a" * 16, "envelope_file_missing")])
    assert d.rule_id != "R7" and any("unreadable evidence" in m for m in d.missing_evidence)


def test_r7_wins_over_r8_when_a_document_exists():
    d = run(r7_features(market_median_move_pct=-6.0), sector_doc())
    assert (d.rule_id, d.confidence) == ("R7", "medium")


# ------------------------------------------------------------------ R8 market stress

def test_r8_market_stress_is_genuine_at_low_confidence():
    d = run(F(gross_factor=0.8, market_median_move_pct=-4.4))
    assert (d.label, d.rule_id, d.confidence, d.subtype, d.resolve_kind, d.factor) == \
        ("genuine_event", "R8", "low", "market_stress", "genuine", 1.0)
    assert run(F(gross_factor=1.45, market_median_move_pct=4.0)).rule_id == "R8"      # 4.0 exactly, same sign, up


def test_r8_negative_cases():
    assert run(F(gross_factor=0.8, market_median_move_pct=-3.9)).rule_id != "R8"
    assert run(F(gross_factor=0.8, market_median_move_pct=5.0)).rule_id != "R8"       # opposite sign
    assert run(F(gross_factor=0.8, market_median_move_pct=None)).rule_id != "R8"
    assert run(F(gross_factor=0.8, market_median_move_pct=-5.0, vendor_split_rows=(VENDOR_ROW,))).rule_id != "R8"
    assert run(F(gross_factor=0.8, market_median_move_pct=-5.0), demerger_item()).rule_id != "R8"   # keyword found


def test_r8_is_not_blocked_by_an_unknown_series():
    assert run(F(gross_factor=0.8, market_median_move_pct=-4.4, series=None)).rule_id == "R8"


# ------------------------------------------------------------------ R5 cross-section

def test_r5_cross_section_takes_the_nearest_round_ratio():
    d = run(F(gross_factor=0.251705, cross_section_count=3, market_median_move_pct=0.26))
    assert (d.label, d.subtype, d.rule_id, d.confidence) == ("vendor_error_other", "misdated_adjustment", "R5", "low")
    assert (d.action, d.resolve_kind, d.factor, d.factor_basis, d.needs_primary) == ("resolve", "split", 4.0, "round_ratio", True)
    assert run(F(gross_factor=0.204968, cross_section_count=3, market_median_move_pct=0.26)).factor == 5.0


def test_r5_without_a_round_ratio_makes_no_call_on_the_factor():
    d = run(F(gross_factor=0.3, cross_section_count=4, market_median_move_pct=0.1))
    assert d.rule_id == "R5" and d.action == "none" and d.factor is None


def test_r5_negative_cases():
    base = dict(gross_factor=0.251705, market_median_move_pct=0.26)
    assert run(F(cross_section_count=2, **base)).rule_id != "R5"
    assert run(F(cross_section_count=3, **{**base, "market_median_move_pct": 2.0})).rule_id != "R5"
    assert run(F(cross_section_count=3, **{**base, "market_median_move_pct": None})).rule_id != "R5"
    assert run(F(cross_section_count=None, **base)).rule_id != "R5"


def test_r8_wins_over_r5_on_a_stressed_day():
    assert run(F(gross_factor=0.7, cross_section_count=5, market_median_move_pct=-11.2)).rule_id == "R8"


# ------------------------------------------------------------------ R6 reversal

def test_r6_reversal_before_the_jump_is_a_one_bar_glitch():
    d = run(F(gross_factor=0.494, nearby_jumps=((-2, "2026-10-26", 2.04),)))
    assert (d.label, d.subtype, d.rule_id, d.action, d.confidence, d.factor) == \
        ("vendor_error_other", "one_bar_glitch", "R6", "none", "low", None)


def test_r6_negative_cases():
    assert run(F(gross_factor=0.494, nearby_jumps=((-2, "2026-10-26", 1.6),))).rule_id != "R6"          # product 0.79
    assert run(F(gross_factor=0.494, nearby_jumps=((-9, "2026-10-19", 2.04),))).rule_id != "R6"         # outside 7 sessions
    assert run(F(gross_factor=0.494)).rule_id != "R6"
    assert run(F(gross_factor=None, nearby_jumps=((-2, "2026-10-26", 2.04),))).rule_id != "R6"


def test_r6_product_bounds_are_inclusive():
    lo, hi = rules.R6_PRODUCT_RANGE
    assert run(F(gross_factor=0.5, nearby_jumps=((-1, "2026-10-27", lo / 0.5),))).rule_id == "R6"
    assert run(F(gross_factor=0.5, nearby_jumps=((-1, "2026-10-27", hi / 0.5),))).rule_id == "R6"


def test_r5_wins_over_r6():
    f = F(gross_factor=0.251705, cross_section_count=3, market_median_move_pct=0.26,
          nearby_jumps=((-1, "2026-10-27", 3.97),))
    assert run(f).rule_id == "R5"


# ------------------------------------------------------------------ day 0 versus day 5

def test_day0_unknown_abstains_with_recheck_and_day5_does_not():
    f0 = F(gross_factor=0.6, n_post_bars=0)
    d = run(f0)
    assert_unknown(d)
    assert d.recheck_on == "2026-11-02"                     # as_of is a Friday: next session is Monday
    assert any("bars after the ex-date" in m for m in d.missing_evidence)
    f5 = F(gross_factor=0.6, n_post_bars=5)
    d5 = run(f5)
    assert_unknown(d5) and d5.recheck_on is None
    assert f0.post_bars_available is False and f5.post_bars_available is True
    assert run(F(n_post_bars=4)).recheck_on == "2026-11-02"


def test_day0_low_confidence_r5_and_r8_carry_a_next_session_recheck_and_day5_does_not():
    r8 = dict(gross_factor=0.8, market_median_move_pct=-4.4)
    r5 = dict(gross_factor=0.251705, cross_section_count=3, market_median_move_pct=0.26)
    for over in (r8, r5):
        d0, d5 = run(F(n_post_bars=0, **over)), run(F(n_post_bars=5, **over))
        assert d0.rule_id == d5.rule_id and d0.confidence == d5.confidence == "low"
        assert d0.recheck_on == "2026-11-02" and d5.recheck_on is None
    # only the price-only calls get one: a document-based R7 result, or an anomaly with no bars to recheck, gets none
    assert run(F(n_post_bars=0), sector_doc()).recheck_on is None
    assert run(F(n_post_bars=None, **r8)).recheck_on is None


def test_an_unmeasured_store_gets_no_next_session_recheck():
    """n_post_bars None means the ex-date bar is not in the store: a later bar would not help."""
    d = run(F(gross_factor=None, n_post_bars=None))
    assert_unknown(d) and d.recheck_on is None
    assert not any("bars after the ex-date" in m for m in d.missing_evidence)
    assert any("price bars for the ex-date" in m for m in d.missing_evidence)


def test_a_jump_seen_after_the_ex_date_fires_r6_only_when_the_bars_exist():
    """INDIAMART 2019-10-27 and its reversal two sessions later: day 0 abstains, day 5 sees the pair."""
    cases = json.loads((FIXTURES / "recorded_cases.json").read_text())["cases"]
    case = next(c for c in cases if c["case_id"] == "INDIAMART_2019-10-27")
    day0 = rules.classify(feat.from_recorded(case, cases, post_bars_available=False))
    day5 = rules.classify(feat.from_recorded(case, cases, post_bars_available=True))
    assert_unknown(day0) and day0.recheck_on == "2019-10-28"
    assert (day5.label, day5.rule_id, day5.subtype) == ("vendor_error_other", "R6", "one_bar_glitch")


def test_placeholder_anomaly_has_no_recheck_date_from_missing_bars():
    f = feat.Features(isin=ISIN, ex_date=EX, as_of=AS_OF, anomaly_kind="index_placeholder", placeholder=True)
    assert run(f).recheck_on == "2026-11-06"               # R1's own seven-day wait


# ------------------------------------------------------------------ the prototype baseline on recorded rows

def _recorded():
    doc = json.loads((FIXTURES / "recorded_cases.json").read_text())
    return doc["cases"], set(doc["placeholder_cases"])


def _replay(post_bars_available: bool):
    cases, placeholders = _recorded()
    out = []
    for c in cases:
        f = feat.from_recorded(c, cases, post_bars_available=post_bars_available, placeholder=c["case_id"] in placeholders)
        out.append((c, rules.classify(f)))
    return out


def test_prototype_baseline_is_reproduced_on_the_recorded_rows_at_day0():
    rows = _replay(False)
    fired = [(c, d) for c, d in rows if d.label != "unknown"]
    correct = [(c, d) for c, d in fired if d.label == c["label_class"]]
    assert len(rows) == 36 and len(fired) == 11 and len(correct) == 11
    assert Counter(d.rule_id for _, d in fired) == {"R8": 5, "R5": 3, "R6": 2, "R1": 1}
    by_label = Counter(c["label_class"] for c, d in rows if d.label == "unknown")
    assert by_label == {"genuine_event": 13, "demerger": 6, "vendor_error_other": 5, "scheme": 1}
    # no value-transfer call and no resolve command at medium or high on a price-only day
    assert all(d.confidence in ("low", "medium") and d.label not in ("demerger", "scheme", "rights") for _, d in fired)
    assert all(d.recheck_on for c, d in rows if d.label == "unknown")          # every day-0 abstention names a recheck date


def test_day5_adds_only_correct_reversal_calls():
    day0 = {c["case_id"]: d for c, d in _replay(False)}
    day5 = _replay(True)
    fired5 = {c["case_id"]: (c, d) for c, d in day5 if d.label != "unknown"}
    assert all(d.label == c["label_class"] for c, d in fired5.values())
    assert {cid for cid in fired5 if day0[cid].label == "unknown"} == {"INDIAMART_2019-10-27", "INDIAMART_2020-11-14"}
    assert len(fired5) == 13


def test_no_dangerous_error_on_the_recorded_rows():
    """Design section 9: a value-transfer proposal on a genuine, pending or vendor case, or genuine on a demerger or scheme."""
    for tier in (False, True):
        for c, d in _replay(tier):
            if d.label in ("demerger", "scheme", "rights"):
                assert c["label_class"] in ("demerger", "scheme", "rights")
            if d.label == "genuine_event" and d.confidence in ("medium", "high"):
                assert c["label_class"] == "genuine_event"
            assert not (d.label == "genuine_event" and c["label_class"] in ("demerger", "scheme"))


def test_recorded_adapter_does_not_read_labels_and_keeps_unknowns_unknown():
    cases, _ = _recorded()
    case = next(c for c in cases if c["case_id"] == "TMPV_2025-10-14")
    f0 = feat.from_recorded(case, cases, post_bars_available=False)
    assert (f0.series, f0.n_peers_moved, f0.next_day_close_ratio, f0.fwd5_close_ratio) == (None, None, None, None)
    assert f0.cross_section_count == 1 and f0.vendor_split_rows == ()
    assert "label" not in " ".join(feat.Features.__dataclass_fields__)
    f5 = feat.from_recorded(case, cases, post_bars_available=True)
    assert f5.next_day_close_ratio == pytest.approx(0.9884) and f5.post_bars_available


def test_tmpv_with_the_circular_is_high_and_without_it_is_unknown():
    """Design section 6 replay: the price-only engine abstains, the SPOS circular gives the exchange-derived factor."""
    cases, _ = _recorded()
    case = next(c for c in cases if c["case_id"] == "TMPV_2025-10-14")
    f = feat.from_recorded(case, cases, post_bars_available=True)
    assert_unknown(rules.classify(f))
    d = rules.classify(f, [spos_item()])
    assert (d.label, d.rule_id, d.factor) == ("demerger", "R2", pytest.approx(1.651875))


# ------------------------------------------------------------------ POLICYBZR

def _policybzr():
    fx = json.loads((FIXTURES / "policybzr.json").read_text())
    f = feat.Features(isin=fx["isin"], ex_date=fx["ex_date"], as_of=fx["as_of"], symbol=fx["symbol"],
                      gross_factor=fx["close_ex"] / fx["close_prev"], n_post_bars=0,
                      cross_section_count=1, **fx["features"], **{})
    f = replace(f, vendor_split_rows=tuple(fx["features"]["vendor_split_rows"]))
    e = dict(fx["regulator_envelope"], schema="triage.envelope/1", subject={"isin": fx["isin"], "ex_date": fx["ex_date"]})
    doc = evidence.EvidenceItem("TD-" + "b" * 16, "b" * 64, "2026-09-24T11:00:00.000000Z", e, True, "ok")
    return fx, f, doc


def test_policybzr_is_genuine_medium_with_the_regulator_paper_and_peers():
    fx, f, doc = _policybzr()
    assert f.gross_factor == pytest.approx(1210.0 / 1886.3) and round(f.gross_factor, 4) == 0.6415
    d = rules.classify(f, [doc])
    assert (d.label, d.rule_id, d.confidence, d.action, d.resolve_kind, d.factor) == \
        ("genuine_event", "R7", "medium", "resolve", "genuine", 1.0)
    assert d.cited == (doc.capture_id,)
    assert rules.classify(replace(f, n_peers_moved=1), [doc]).confidence == "low"


def test_policybzr_is_unknown_without_the_document():
    fx, f, doc = _policybzr()
    d = rules.classify(f, [])
    assert_unknown(d)
    assert any("primary document" in m for m in d.missing_evidence)
    assert d.recheck_on == "2026-09-25"                      # Thursday after a Wednesday as_of
    assert any("special pre-open price" in m for m in d.missing_evidence)


# ------------------------------------------------------------------ features: flat mapping

def test_flat_keeps_unknown_apart_from_false_and_empty():
    unknown = feat.flat(feat.Features(isin=ISIN, ex_date=EX, as_of=AS_OF, gross_factor=0.6))
    assert unknown["vendor_split_rows"] is None and unknown["placeholder"] is None and unknown["series"] is None
    assert unknown["n_peers_moved"] is None and unknown["reversal_forward"] is None and unknown["market_median_move_pct"] is None
    known = feat.flat(F(n_post_bars=5))
    assert known["vendor_split_rows"] == [] and known["placeholder"] is False
    assert known["reversal_prior"] is False and known["reversal_forward"] is False and known["peer_set_pit"] is False
    assert feat.flat(F(n_post_bars=0))["reversal_forward"] is None       # day 0: the forward half is unknown, not false


def test_flat_carries_the_keys_the_validator_reads():
    keys = set(feat.flat(F()))
    assert {"gross_factor", "n_peers_moved", "sector_peer_median_move_pct", "market_median_move_pct", "vendor_split_rows",
            "cross_section_count", "reversal_prior", "placeholder", "series"} <= keys


def test_jump_test_matches_actions_detect_bounds():
    jr = 1.4
    assert feat.is_jump(1.4001, jr) and feat.is_jump(1 / 1.4001, jr) and not feat.is_jump(1.4, jr) and not feat.is_jump(1 / 1.4, jr)
    assert not feat.is_jump(float("nan"), jr)
    assert feat.jump_ratio_from(load_config()) == 1.4


# ------------------------------------------------------------------ disposable worlds

BDAYS = pd.bdate_range("2026-06-01", AS_OF).strftime("%Y-%m-%d").tolist()
OBS = lambda d: f"{d}T13:00:00.000000Z"                                       # noqa: E731  bar observed that evening
PRICE_COLS = ("security_id, date, open_raw, high_raw, low_raw, close_raw, volume_raw, dividend_raw, split_ratio, "
              "yahoo_close, yahoo_adj_close, observed_at, capture_id, source_sha256")


def add_bars(path, sid, dates, closes, *, observed_at=None, volume=1e5, split=1.0):
    con = sqlite3.connect(path)
    con.executemany(
        f"INSERT INTO prices_daily ({PRICE_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(sid, d, c, c, c, float(c), volume, 0.0, split, c, c, observed_at or OBS(d), "CAP", "SHA")
         for d, c in zip(dates, closes)])
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def templates(tmp_path_factory):
    root = tmp_path_factory.mktemp("triage_rules")
    state, prices = root / "state.tpl.db", root / "prices.tpl.db"
    conn = connect(state)
    apply_schema(conn, kind="state")
    for sid in range(1, 11):
        grp = "Insurance" if sid <= 6 else "Metals"
        conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                     "VALUES (?, ?, ?, '2026-01-01', '2026-10-30', 'listed')", (sid, f"INE{sid:09d}", f"Company {sid}"))
        conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                     "VALUES (?, ?, ?, '2026-01-01', 'test')", (sid, f"SYM{sid}", f"SYM{sid}.NS"))
        conn.execute("INSERT INTO sector_map (security_id, observed_at, valid_from, valid_to, nse_sector, yahoo_sector, "
                     "yahoo_industry, sector_group, group_def_version, source, confidence) "
                     "VALUES (?, '2026-09-01T00:00:00.000000Z', '2026-01-01', NULL, 'S', 'S', 'S', ?, 1, 'current_backfill', 0.5)",
                     (sid, grp))
        conn.execute("INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, nse_sector, "
                     "series, source) VALUES ('2026-10-01', '2026-10-01T10:00:00.000000Z', ?, 'NIFTY500', ?, 'S', 'EQ', 'nse_csv')",
                     (sid, f"SYM{sid}"))
    conn.close()
    PriceStore(prices, state_conn=connect(state)).path  # creates the schema
    rng = np.random.default_rng(7)
    n = len(BDAYS)
    market = rng.normal(0, 0.008, n)
    factor = {"Insurance": rng.normal(0, 0.01, n), "Metals": rng.normal(0, 0.01, n)}
    i_ex = BDAYS.index(EX)
    for sid in range(1, 11):
        grp = "Insurance" if sid <= 6 else "Metals"
        r = market + 0.8 * factor[grp] + rng.normal(0, 0.004, n)
        close = 100.0 * np.cumprod(1.0 + r)
        if sid == 1:
            close[i_ex:] *= 0.6                                  # the anomaly: a 40% one-day step
        if sid in (2, 3):
            close[i_ex:] *= 0.94 / (1.0 + r[i_ex])               # two sector peers fall 6% the same day
        add_bars(prices, sid, BDAYS, close)
    return state, prices


class World:
    def __init__(self, root: Path, templates_):
        state_tpl, prices_tpl = templates_
        self.root = root
        shutil.copy(state_tpl, root / "state.db")
        shutil.copy(prices_tpl, root / "p.db")
        self.cfg = load_config().with_paths(db=root / "state.db", prices_db=root / "p.db", data_dir=root / "d",
                                            archive_dir=root / "a", knowledge_dir=root / "k", ui_dir=root / "u")

    def run(self, now: str = NOON, actor_kind: str = "system"):
        return RunContext(as_of=AS_OF, kind="triage", track="live", cfg=self.cfg, clock=FrozenClock(now),
                          actor=Actor(kind=actor_kind, name="pytest"))

    def build_all(self, conn):
        st = PriceStore.open_readonly(self.cfg.paths.prices_db, conn)
        res = scan.scan(conn, st, AS_OF, jump_ratio=1.4)
        reader = feat.Reader(conn, st, AS_OF, jump_ratio=1.4)
        return [pack.build_one(conn, reader, a, archive_dir=self.cfg.paths.archive_dir) for a in res.anomalies]

    def build(self):
        conn = connect(self.cfg.paths.db, readonly=True)
        try:
            built = self.build_all(conn)
        finally:
            conn.close()
        assert len(built) == 1
        return built[0]


@pytest.fixture
def world(tmp_path, templates):
    return World(tmp_path, templates)


def register(world, **over):
    fields = dict(provenance="human_attach", subject={"isin": "INE000000001", "ex_date": EX}, url=None,
                  title="Regulator consultation on distribution", published_at="2026-10-27", published_at_basis="human_declared",
                  source_class="regulator", relevance="sector", relevance_basis="attacher declared",
                  excerpt="SYNTHETIC excerpt about distribution commissions")
    fields.update(over)
    with world.run() as ctx:
        return evidence.register(ctx, fields)


# ------------------------------------------------------------------ pack: features from the stores

def test_build_one_reads_features_from_the_stores(world):
    b = world.build()
    f = b.features
    assert (b.key, b.anomaly.kind, b.anomaly.symbol, b.anomaly.status) == ("INE000000001@2026-10-28", "suspected_jump", "SYM1", "new")
    assert f.gross_factor == pytest.approx(0.6, abs=0.03)
    assert f.series == "EQ" and f.placeholder is False and f.vendor_split_rows == () and f.cross_section_count == 1
    assert f.n_post_bars == 2 and f.post_bars_available is False
    assert f.sector_group == "Insurance" and f.n_peers == 5 and f.n_peers_moved == 2
    assert f.market_median_move_pct is not None and abs(f.market_median_move_pct) < 2.0
    assert f.volume_ratio == pytest.approx(1.0) and f.next_day_close_ratio is not None and f.fwd5_close_ratio is None
    assert f.peer_set_pit is False and b.pack["features"]["peer_set_pit"] is False
    assert_unknown(b.decision) and b.decision.recheck_on == "2026-11-02"
    assert b.pack["source_status"]["yahoo_actions"] == "skipped" and b.pack["source_status"]["price_store"] == "ok"


def test_pack_and_proposal_are_well_formed_and_within_caps(world):
    b = world.build()
    assert b.pack["schema"] == "triage.pack/1" and b.pack["cutoff_utc"] == CUTOFF and b.pack["rules_version"] == rules.RULES_VERSION
    assert len(b.pack_raw) <= 12 * 1024 and len(json.dumps(b.proposal)) <= 8 * 1024
    assert check_schema(b.proposal, producer_kind="engine") == []
    assert b.proposal["producer"]["pack_sha256"] == b.pack_sha256 and b.proposal["producer"]["name"] == "rules:triage-rules/1"
    assert b.proposal["classification"]["label"] == "unknown" and b.proposal["resolution"]["recheck_on"] == "2026-11-02"


def test_a_document_turns_the_same_anomaly_into_a_medium_genuine_proposal(world):
    reg = register(world)
    b = world.build()
    c = b.proposal["classification"]
    assert (c["label"], c["rule_id"], c["confidence"]) == ("genuine_event", "R7", "medium")      # Ps plus two moving peers
    assert b.proposal["resolution"]["action"] == "resolve" and b.proposal["resolution"]["factor"] == 1.0
    assert [x["capture_id"] for x in b.proposal["citations"]] == [reg.capture_id]
    assert b.evidence == [{"capture_id": reg.capture_id, "sha256": reg.sha256}]
    assert all(ch["result"] == "none_found" for ch in b.proposal["negative_checks"])
    assert b.pack["source_status"]["human_attach"] == "ok"


def _add_security(world, sid, closes_by_date):
    conn = connect(world.cfg.paths.db)
    conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                 "VALUES (?, ?, ?, '2026-01-01', '2026-10-30', 'listed')", (sid, f"INE{sid:09d}", f"Company {sid}"))
    conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                 "VALUES (?, ?, ?, '2026-01-01', 'test')", (sid, f"SYM{sid}", f"SYM{sid}.NS"))
    conn.close()
    dates = sorted(closes_by_date)
    add_bars(world.cfg.paths.prices_db, sid, dates, [closes_by_date[d] for d in dates])


def test_a_reversal_found_in_the_store_fires_r6_on_both_jumps_before_day5(world):
    bars = {d: 100.0 for d in BDAYS}
    bars["2026-10-27"] = 200.0                     # one bad bar: up 2x, then straight back
    _add_security(world, 11, bars)
    conn = connect(world.cfg.paths.db, readonly=True)
    built = {b.anomaly.ex_date: b for b in world.build_all(conn) if b.anomaly.security_id == 11}
    assert sorted(built) == ["2026-10-27", "2026-10-28"]
    second, first = built["2026-10-28"], built["2026-10-27"]
    assert second.features.nearby_jumps[0][0] == -1 and second.decision.rule_id == "R6"
    assert first.features.nearby_jumps[0][0] == 1 and first.features.post_bars_available is False
    assert first.decision.rule_id == "R6"          # the later jump is visible in the bars even though day 5 has not come
    assert first.pack["features"]["reversal_forward"] is True and second.pack["features"]["reversal_prior"] is True


def test_an_anomaly_with_no_bars_keeps_every_store_feature_unknown(world):
    conn = connect(world.cfg.paths.db, readonly=True)
    st = PriceStore.open_readonly(world.cfg.paths.prices_db, conn)
    reader = feat.Reader(conn, st, AS_OF, jump_ratio=1.4)
    old = scan.Anomaly("suspected_jump", ISIN, "2026-01-05", 1, "SYM1", 0.62, "2026-01-05T13:00:00.000000Z", "suspect_row", False)
    b = pack.build_one(conn, reader, old)
    f = b.features
    assert (f.gross_factor, f.n_post_bars, f.vendor_split_rows, f.volume_ratio, f.next_day_close_ratio) == (0.62, None, None, None, None)
    assert f.nearby_jumps == () and f.cross_section_count is None and f.market_median_move_pct is None
    assert b.pack["features"]["vendor_split_rows"] is None and b.pack["source_status"]["price_store"] == "empty"
    assert_unknown(b.decision) and "vendor split record near the ex-date" in b.decision.missing_evidence


# ------------------------------------------------------------------ pack: point in time and idempotence

def _append_bars(world, sid, dates, closes, **kw):
    add_bars(world.cfg.paths.prices_db, sid, dates, closes, **kw)


def test_pack_hash_is_unchanged_when_later_bars_are_added(world):
    before = world.build()
    last = 100.0
    _append_bars(world, 1, ["2026-11-02", "2026-11-03"], [last, last * 1.5])                    # dated after as_of
    _append_bars(world, 1, ["2026-10-30"], [999.0], observed_at="2026-10-31T10:00:00.000000Z")  # revision seen after the cutoff
    _append_bars(world, 4, ["2026-10-28"], [1.0], observed_at="2026-11-01T10:00:00.000000Z")    # a peer revised after the cutoff
    _append_bars(world, 1, ["2026-11-04"], [5.0], observed_at="2026-10-29T10:00:00.000000Z")    # dated later, claimed earlier
    after = world.build()
    assert after.pack_sha256 == before.pack_sha256 and after.pack_raw == before.pack_raw
    assert after.proposal == before.proposal


def test_pack_hash_changes_when_a_bar_within_the_window_changes(world):
    before = world.build()
    con = sqlite3.connect(world.cfg.paths.prices_db)
    close = con.execute("SELECT close_raw FROM prices_daily WHERE security_id = 1 AND date = '2026-10-30'").fetchone()[0]
    con.close()
    _append_bars(world, 1, ["2026-10-30"], [close * 1.01], observed_at="2026-10-30T14:00:00.000000Z")   # revised before the cutoff
    assert world.build().pack_sha256 != before.pack_sha256


def test_build_is_idempotent_and_supersedes_only_when_the_payload_changes(world):
    def write(ctx, built):
        return triage_store.write_proposal(ctx, built.proposal, built.evidence)

    first = world.build()
    with world.run() as ctx:
        a = write(ctx, first)
    assert a.created
    again = world.build()
    assert again.pack_sha256 == first.pack_sha256
    with world.run() as ctx:
        b = write(ctx, again)
        n = ctx.conn.execute("SELECT count(*) FROM proposals").fetchone()[0]
    assert (b.created, b.proposal_id, n) == (False, a.proposal_id, 1)
    register(world)
    changed = world.build()
    assert changed.pack_sha256 != first.pack_sha256
    with world.run() as ctx:
        c = write(ctx, changed)
        states = dict(ctx.conn.execute("SELECT proposal_id, status FROM proposals").fetchall())
    assert c.created and c.superseded == [a.proposal_id]
    assert states == {a.proposal_id: "superseded", c.proposal_id: "proposed"}


def test_evidence_captured_after_the_cutoff_is_absent_from_the_pack(world):
    with world.run(now="2026-10-30T18:30:00.000000Z") as ctx:          # one microsecond past the cutoff is 18:29:59.999999
        late = evidence.register(ctx, dict(provenance="human_attach", subject={"isin": ISIN, "ex_date": EX}, title="Late note",
                                           source_class="company", relevance="entity", published_at="2026-10-27",
                                           published_at_basis="human_declared", entity_match=True))
    b = world.build()
    assert late.capture_id not in {e["capture_id"] for e in b.pack["evidence"]}
    assert (late.capture_id, "captured_after_cutoff") in [(r["capture_id"], r["reason"]) for r in b.pack["rejected_evidence"]]
    assert_unknown(b.decision)


def test_pack_and_evidence_lists_stay_within_their_caps_with_many_documents(world):
    words = " ".join(f"word{i}" for i in range(48))
    with world.run() as ctx:                                            # one run: the test is about sizes, not runs
        for k in range(22):
            evidence.register(ctx, dict(
                provenance="human_attach", subject={"isin": ISIN, "ex_date": EX}, title=f"Notice number {k} " + "x" * 200,
                excerpt=words, url="https://example.test/" + "u" * 140 + str(k), source_class="news", relevance="context",
                published_at=f"2026-10-{10 + k % 18:02d}", published_at_basis="human_declared"))
    b = world.build()
    assert len(b.pack_raw) <= 12 * 1024
    assert len(b.pack["evidence"]) == len(b.evidence) <= pack.MAX_EVIDENCE_ITEMS
    assert b.pack["evidence_omitted"] == 22 - len(b.pack["evidence"]) >= 6
    assert len(json.dumps(b.evidence, separators=(",", ":"))) <= 2048
    with world.run() as ctx:
        triage_store.write_proposal(ctx, b.proposal, b.evidence)        # the store's own caps accept it


def test_fit_drops_documents_whose_typed_fields_cannot_fit_and_keeps_the_pack_within_the_cap():
    big = {f"k{i}": "v" * 90 for i in range(16)}                       # about 2 KB of typed fields per document
    items = [item(typed_fields=big, title=f"Notice {k}") for k in range(10)]
    out = pack._fit({"subject": {"key": "k"}}, items)
    assert len(pack.canonical_bytes(out)) <= pack.MAX_PACK_BYTES
    assert out["evidence_omitted"] == len(items) - len(out["evidence"]) > 0


def test_fit_refuses_a_pack_that_does_not_fit_even_with_no_documents():
    with pytest.raises(Exception) as ei:
        pack._fit({"features": {"blob": "x" * (pack.MAX_PACK_BYTES + 1)}}, [item()])
    assert getattr(ei.value, "code", "") == "triage_pack_too_large"


def test_write_pack_file_is_idempotent_and_refuses_a_different_file(world):
    b = world.build()
    path = pack.write_pack_file(world.cfg.paths.data_dir, "PT-2026-10-30-001", b)
    assert path.read_bytes() == b.pack_raw and path == pack.pack_path(world.cfg.paths.data_dir, "PT-2026-10-30-001")
    assert pack.write_pack_file(world.cfg.paths.data_dir, "PT-2026-10-30-001", b) == path
    path.write_bytes(b"{}")
    with pytest.raises(Exception) as ei:
        pack.write_pack_file(world.cfg.paths.data_dir, "PT-2026-10-30-001", b)
    assert getattr(ei.value, "code", "") == "triage_pack_conflict"


def test_a_placeholder_event_folds_into_the_jump_and_fires_r1(world):
    with world.run() as ctx:
        ctx.conn.execute(
            "INSERT INTO data_quality_events (run_id, as_of, created_at, severity, code, security_id, detail_json) "
            "VALUES (?, ?, '2026-10-29T12:00:00.000000Z', 'WARN', 'INDEX_PLACEHOLDER', 1, ?)",
            (ctx.run_id, AS_OF, json.dumps({"placeholder_symbol": "DUMMYSYM1", "placeholder_isin": "INE999999999",
                                            "parent_symbol": "SYM1"})))
    b = world.build()                                         # one anomaly, not two
    assert (b.features.placeholder, b.decision.rule_id, b.decision.action) == (True, "R1", "wait")
    assert b.proposal["resolution"]["recheck_on"] == "2026-11-06"


# ------------------------------------------------------------------ scan

def test_scan_lists_a_placeholder_for_a_security_with_no_jump(world):
    with world.run() as ctx:
        ctx.conn.execute(
            "INSERT INTO data_quality_events (run_id, as_of, created_at, severity, code, security_id, detail_json) "
            "VALUES (?, ?, '2026-10-29T12:00:00.000000Z', 'WARN', 'INDEX_PLACEHOLDER', 7, ?)",
            (ctx.run_id, "2026-09-30", json.dumps({"placeholder_symbol": "DUMMYSYM7", "placeholder_isin": "INE999999998"})))
        ctx.conn.execute(      # a later capture repeats it; a post-cutoff event is invisible
            "INSERT INTO data_quality_events (run_id, as_of, created_at, severity, code, security_id, detail_json) "
            "VALUES (?, ?, '2026-10-31T12:00:00.000000Z', 'WARN', 'INDEX_PLACEHOLDER', 8, '{}')", (ctx.run_id, AS_OF))
    conn = connect(world.cfg.paths.db, readonly=True)
    res = scan.scan(conn, PriceStore.open_readonly(world.cfg.paths.prices_db, conn), AS_OF, jump_ratio=1.4)
    kinds = [(a.kind, a.isin, a.ex_date, a.source) for a in res.anomalies]
    assert ("index_placeholder", "INE000000007", "2026-10-29", "placeholder_event") in kinds
    assert not any(a.security_id == 8 for a in res.anomalies)
    assert sum(1 for a in res.anomalies if a.kind == "suspected_jump") == 1
    b = world.build_all(conn)
    ph = next(x for x in b if x.anomaly.kind == "index_placeholder")
    assert ph.decision.rule_id == "R1" and ph.features.gross_factor is None and ph.pack["source_status"]["price_store"] == "skipped"
    assert check_schema(ph.proposal, producer_kind="engine") == []


def test_scan_status_follows_the_stored_proposal(world):
    b = world.build()
    with world.run() as ctx:
        triage_store.write_proposal(ctx, b.proposal, b.evidence)
    conn = connect(world.cfg.paths.db, readonly=True)
    a = scan.scan(conn, PriceStore.open_readonly(world.cfg.paths.prices_db, conn), AS_OF, jump_ratio=1.4).anomalies[0]
    assert a.status == "proposed" and a.proposal_id and a.key == "INE000000001@2026-10-28"


ACTIONS_CLI = Path(__file__).resolve().parent / "test_wp_actions_cli.py"


def _actions_cli_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("actions_cli_world", ACTIONS_CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _actions_cli_world(tmp_path):
    """The exact fixture world of test_wp_actions_cli: one 40% step on 2025-10-14, detected by actions.detect."""
    mod = _actions_cli_module()
    cfg, db = mod._world(tmp_path)
    mod._seed_suspect(cfg)
    return cfg, db, mod


def test_scan_output_equals_actions_detect_on_the_actions_cli_fixture_world(tmp_path):
    cfg, db, mod = _actions_cli_world(tmp_path)
    conn = connect(db, readonly=True)
    detected = [(int(r[0]), r[1], round(float(json.loads(r[3])["gross_factor"]), 6), r[2]) for r in conn.execute(
        "SELECT security_id, ex_date, observed_at, note FROM corporate_actions WHERE kind = 'suspected' ORDER BY security_id, ex_date")]
    assert len(detected) == 1
    st = PriceStore.open_readonly(cfg.paths.prices_db, conn)
    jumps = scan.measure_jumps(st, [1], since="2025-01-01", through="2026-09-11", vintage_at="2026-09-14T13:58:30.000000Z",
                               jump_ratio=feat.jump_ratio_from(cfg))
    assert [(j.security_id, j.ex_date, round(j.gross_factor, 6), j.observed_at) for j in jumps] == detected
    res = scan.scan(conn, st, "2026-09-11", jump_ratio=feat.jump_ratio_from(cfg))          # every security in the store
    assert [(a.security_id, a.ex_date, round(a.gross_factor, 6), a.observed_at) for a in res.anomalies] == detected
    a = res.anomalies[0]
    assert (a.kind, a.isin, a.symbol, a.source, a.in_window, a.status) == \
        ("suspected_jump", mod.ISIN, mod.SYMBOL, "measured+suspect_row", True, "new")


def test_scan_window_is_thirteen_months_plus_old_suspects_and_since_widens(tmp_path):
    cfg, db, mod = _actions_cli_world(tmp_path)
    conn = connect(db, readonly=True)
    st = PriceStore.open_readonly(cfg.paths.prices_db, conn)
    jr = feat.jump_ratio_from(cfg)
    assert scan.default_window_start("2026-11-30") == "2025-10-30"
    # as_of 2027-12-31: the 2025-10-14 jump is outside the 13 months, but its unresolved suspect row stays listed
    late = scan.scan(conn, st, "2027-12-31", jump_ratio=jr)
    assert [(a.ex_date, a.source, a.in_window) for a in late.anomalies] == [("2025-10-14", "suspect_row", False)]
    assert late.anomalies[0].gross_factor == pytest.approx(0.6, abs=1e-3)
    wide = scan.scan(conn, st, "2027-12-31", since="2025-01-01", jump_ratio=jr)
    assert [(a.ex_date, a.source, a.in_window) for a in wide.anomalies] == [("2025-10-14", "measured+suspect_row", True)]
    assert wide.window_start == "2025-01-01"
    assert scan.scan(conn, st, "2027-12-31", since="2027-06-01", jump_ratio=jr).window_start == scan.default_window_start("2027-12-31")


def _seed_without_suspect(cfg, mod):
    """The actions CLI world's security and bars, but no ``detect`` run: the jump is measured only."""
    dates, close = mod._series()
    with RunContext(as_of="2026-09-11", kind="test", track="live", cfg=cfg, clock=FrozenClock("2026-09-14T13:58:30.000000Z"),
                    actor=Actor(kind="human", name="owner")) as ctx:
        ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                         "VALUES (1, ?, 'P', '2025-01-01', '2026-09-11', 'listed')", (mod.ISIN,))
        ctx.conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                         "VALUES (1, ?, ?, '2025-01-01', 'test')", (mod.SYMBOL, f"{mod.SYMBOL}.NS"))
        store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        store.ingest(ctx, pd.DataFrame({"date": dates, "close": close, "volume": 1e5}),
                     {"security_id": 1, "close_basis": "raw", "observed_at": "2026-08-01T10:00:00.000000Z"})


def test_scan_lists_a_measured_only_jump_ten_months_old_and_omits_one_fifteen_months_old(tmp_path):
    """The TMPV, VEDL and TRENT case (amendment F2): a measured jump with no suspect row, well past 35 days."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("actions_cli_world", ACTIONS_CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg, db = mod._world(tmp_path)
    _seed_without_suspect(cfg, mod)
    conn = connect(db, readonly=True)
    assert conn.execute("SELECT count(*) FROM corporate_actions").fetchone()[0] == 0
    st = PriceStore.open_readonly(cfg.paths.prices_db, conn)
    jr = feat.jump_ratio_from(cfg)
    recent = scan.scan(conn, st, "2026-08-14", jump_ratio=jr)          # the jump is 10 months before as_of
    assert [(a.ex_date, a.source, a.in_window, a.status) for a in recent.anomalies] == [("2025-10-14", "measured", True, "new")]
    old = scan.scan(conn, st, "2027-01-14", jump_ratio=jr)             # 15 months before as_of: outside the window
    assert old.anomalies == [] and old.window_start == "2025-12-14"
    widened = scan.scan(conn, st, "2027-01-14", since="2025-10-01", jump_ratio=jr)
    assert [(a.ex_date, a.source) for a in widened.anomalies] == [("2025-10-14", "measured")]


def test_scan_omits_a_resolved_jump_unless_asked_and_reads_resolution_at_the_cutoff(tmp_path):
    cfg, db, mod = _actions_cli_world(tmp_path)
    with RunContext(as_of="2026-09-11", kind="test", track="live", cfg=cfg, clock=FrozenClock("2026-09-11T12:00:00.000000Z"),
                    actor=Actor(kind="human", name="owner")) as ctx:
        ctx.conn.execute(
            "INSERT INTO corporate_actions (security_id, ex_date, kind, ratio, amount_inr, adj_factor, source, observed_at, "
            "decision_id, note) VALUES (1, '2025-10-14', 'demerger', NULL, NULL, 1.6667, 'manual', "
            "'2026-09-12T01:00:00.000000Z', 'DEC_TEST', NULL)")
    conn = connect(db, readonly=True)
    st = PriceStore.open_readonly(cfg.paths.prices_db, conn)
    jr = feat.jump_ratio_from(cfg)
    # the decision was recorded at 2026-09-12T01:00Z: after the 2026-09-11 cutoff, before the 2026-09-12 one
    assert [a.ex_date for a in scan.scan(conn, st, "2026-09-11", jump_ratio=jr).anomalies] == ["2025-10-14"]
    assert scan.scan(conn, st, "2026-09-12", jump_ratio=jr).anomalies == []
    listed = scan.scan(conn, st, "2026-09-12", jump_ratio=jr, include_resolved=True).anomalies
    assert [(a.ex_date, a.status) for a in listed] == [("2025-10-14", "resolved")]


def test_scan_writes_nothing_and_leaves_the_files_alone(tmp_path):
    cfg, db, mod = _actions_cli_world(tmp_path)
    files = [Path(db), Path(cfg.paths.prices_db)]
    before = [(p.stat().st_mtime_ns, p.stat().st_size) for p in files]
    conn = connect(db, readonly=True)
    scan.scan(conn, PriceStore.open_readonly(cfg.paths.prices_db, conn), "2026-09-11", jump_ratio=1.4)
    assert [(p.stat().st_mtime_ns, p.stat().st_size) for p in files] == before


def test_scan_and_reader_require_the_jump_ratio_so_a_caller_cannot_drift_from_detect(tmp_path):
    cfg, db, mod = _actions_cli_world(tmp_path)
    conn = connect(db, readonly=True)
    st = PriceStore.open_readonly(cfg.paths.prices_db, conn)
    with pytest.raises(TypeError):
        scan.scan(conn, st, "2026-09-11")
    with pytest.raises(TypeError):
        feat.Reader(conn, st, "2026-09-11")


def _seed_many(cfg, mod):
    """Six securities, each with one step on 2026-03-10. Steps: 1 just above 1.40, 2 just below 1.40, 3 exactly 1.40,
    4 a mirror-image drop beyond 1/1.40, 5 a dividend that explains a raw drop, 6 a jump whose key is already in corporate_actions."""
    dates = pd.bdate_range("2025-12-01", "2026-09-11").strftime("%Y-%m-%d")
    k = list(dates).index("2026-03-10")
    steps = {1: 840.01 / 600.0, 2: 839.0 / 600.0, 3: 840.0 / 600.0, 4: 400.0 / 600.0,
             5: 400.0 / 600.0, 6: 1.6}
    with RunContext(as_of="2026-09-11", kind="test", track="live", cfg=cfg, clock=FrozenClock("2026-09-14T13:58:30.000000Z"),
                    actor=Actor(kind="human", name="owner")) as ctx:
        store = PriceStore(cfg.paths.prices_db, state_conn=ctx.conn)
        for sid, step in steps.items():
            isin = f"INE00000000{sid}"
            ctx.conn.execute("INSERT INTO securities (security_id, isin, name, first_seen, last_seen, status) "
                             "VALUES (?, ?, 'P', '2025-01-01', '2026-09-11', 'listed')", (sid, isin))
            ctx.conn.execute("INSERT INTO symbol_history (security_id, nse_symbol, yahoo_ticker, valid_from, source) "
                             "VALUES (?, ?, ?, '2025-01-01', 'test')", (sid, f"SYM{sid}", f"SYM{sid}.NS"))
            close = np.full(len(dates), 600.0)
            close[k:] = 600.0 * step
            frame = {"date": dates, "close": close, "volume": 1e5}
            if sid == 5:                                            # raw drop of a third, but a 200 INR dividend that day
                frame["dividend"] = np.where(np.arange(len(dates)) == k, 200.0, 0.0)
            store.ingest(ctx, pd.DataFrame(frame), {"security_id": sid, "close_basis": "raw",
                                                    "observed_at": "2026-08-01T10:00:00.000000Z"})
        ctx.conn.execute("INSERT INTO corporate_actions (security_id, ex_date, kind, source, observed_at, decision_id, note) "
                         "VALUES (6, '2026-03-10', 'split', 'yahoo_actions', '2026-08-01T10:00:00.000000Z', NULL, NULL)")
        ctx.store = store
        res = actions.detect(ctx, sorted(steps), since="2025-12-01")
        ctx.status = "ok"
    return res


def test_scan_equals_detect_across_securities_thresholds_dividends_and_known_keys(tmp_path):
    """Where the brief says they match (every unknown key) scan equals detect; they differ only on a known undecided key."""
    mod = _actions_cli_module()
    (tmp_path / "many").mkdir()
    cfg, db = mod._world(tmp_path / "many")
    res = _seed_many(cfg, mod)
    conn = connect(db, readonly=True)
    detected = {(int(r[0]), r[1]): round(float(json.loads(r[2])["gross_factor"]), 6) for r in conn.execute(
        "SELECT security_id, ex_date, note FROM corporate_actions WHERE kind = 'suspected'")}
    # detect flags 1 (above 1.40) and 4 (below 1/1.40); not 2, not 3 (exactly 1.40), not 5 (dividend), not 6 (key known)
    assert sorted(detected) == [(1, "2026-03-10"), (4, "2026-03-10")] and res.counts["suspected"] == 2
    st = PriceStore.open_readonly(cfg.paths.prices_db, conn)
    jr = feat.jump_ratio_from(cfg)
    got = scan.scan(conn, st, "2026-09-11", jump_ratio=jr)
    listed = {(a.security_id, a.ex_date): (round(a.gross_factor, 6), a.source) for a in got.anomalies}
    # every key detect wrote is listed with the same factor
    for key, g in detected.items():
        assert listed[key][0] == g and listed[key][1] == "measured+suspect_row"
    # the undecided non-suspect row: detect skips the key, scan still lists it (the jump distorts the series until a decision)
    assert (6, "2026-03-10") not in detected and listed[(6, "2026-03-10")][1] == "measured"
    # inside the band, exactly on it, and explained by a dividend: neither lists them
    for sid in (2, 3):
        assert (sid, "2026-03-10") not in listed and (sid, "2026-03-10") not in detected
    assert set(listed) - {(6, "2026-03-10")} == set(detected)


# ------------------------------------------------------------------ R-2: a price store without an action source is refused at open

def test_open_readonly_without_a_state_connection_is_refused_at_open_time(world):
    with pytest.raises(Refused) as ei:
        PriceStore.open_readonly(world.cfg.paths.prices_db)
    assert ei.value.code == "action_source_missing"


def test_open_readonly_refuses_before_looking_at_the_file(tmp_path):
    """The contract check does not depend on the environment: a missing file does not hide it."""
    with pytest.raises(Refused) as ei:
        PriceStore.open_readonly(tmp_path / "nope.db")
    assert ei.value.code == "action_source_missing"


def test_open_readonly_with_a_state_connection_needs_no_flag_and_has_an_action_source(world):
    conn = connect(world.cfg.paths.db, readonly=True)
    try:
        store = PriceStore.open_readonly(world.cfg.paths.prices_db, conn)
        assert store.has_action_source is True and store.require_action_source() is store
        with store.conn() as pc:
            assert pc.execute("SELECT count(*) FROM prices_daily").fetchone()[0] > 0
    finally:
        conn.close()


def test_open_readonly_allow_unadjusted_is_an_explicit_opt_out_that_stays_visible(world):
    store = PriceStore.open_readonly(world.cfg.paths.prices_db, allow_unadjusted=True)
    assert store.has_action_source is False
    with pytest.raises(Refused) as ei:
        store.require_action_source()
    assert ei.value.code == "action_source_missing"
    with store.conn() as pc:                                   # raw reads work
        assert pc.execute("SELECT count(*) FROM prices_daily").fetchone()[0] > 0


def test_allow_unadjusted_does_not_hide_a_state_connection(world):
    conn = connect(world.cfg.paths.db, readonly=True)
    try:
        assert PriceStore.open_readonly(world.cfg.paths.prices_db, conn, allow_unadjusted=True).has_action_source is True
    finally:
        conn.close()


def test_allow_unadjusted_is_keyword_only_and_defaults_to_false(world):
    import inspect

    p = inspect.signature(PriceStore.open_readonly).parameters["allow_unadjusted"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is False
    with pytest.raises(TypeError):
        PriceStore.open_readonly(world.cfg.paths.prices_db, None, True)


# ------------------------------------------------------------------ R-7: placeholder window is [ex - 45 days, ex + 15 days]

@pytest.mark.parametrize("event_day, attached", [
    ("2026-09-12", False),        # ex - 46 days
    ("2026-09-13", True),         # ex - 45 days
    ("2026-10-28", True),         # the ex-date
    ("2026-11-12", True),         # ex + 15 days
    ("2026-11-13", False),        # ex + 16 days: a later placeholder is not this jump's
])
def test_placeholder_for_attaches_only_events_inside_the_window(event_day, attached):
    events = [{"event_id": 1, "day": event_day, "security_id": 1, "detail": {}}]
    got = feat.placeholder_for(events, security_id=1, isin=ISIN, symbol="TESTCO", ex_date=EX)
    assert got is attached                                    # False is a measured "no", not unknown


def test_placeholder_for_keeps_unknown_apart_from_no_and_matches_by_isin_or_parent_symbol():
    assert feat.placeholder_for(None, security_id=1, isin=ISIN, symbol="TESTCO", ex_date=EX) is None
    assert feat.placeholder_for([], security_id=1, isin=ISIN, symbol="TESTCO", ex_date=EX) is False
    by_isin = [{"day": EX, "security_id": 99, "detail": {"placeholder_isin": ISIN}}]
    by_parent = [{"day": EX, "security_id": 99, "detail": {"parent_symbol": "TESTCO"}}]
    assert feat.placeholder_for(by_isin, security_id=1, isin=ISIN, symbol="TESTCO", ex_date=EX) is True
    assert feat.placeholder_for(by_parent, security_id=1, isin=ISIN, symbol="TESTCO", ex_date=EX) is True


def _placeholder_event(ctx, sid, day, detail=None):
    ctx.conn.execute(
        "INSERT INTO data_quality_events (run_id, as_of, created_at, severity, code, security_id, detail_json) "
        "VALUES (?, ?, ?, 'WARN', 'INDEX_PLACEHOLDER', ?, ?)",
        (ctx.run_id, AS_OF, f"{day}T12:00:00.000000Z", sid, json.dumps(detail or {"placeholder_isin": "INE999999997"})))


def test_a_later_out_of_window_placeholder_does_not_attach_to_an_old_jump(world):
    old = {d: (100.0 if d < "2026-09-14" else 50.0) for d in BDAYS}        # one jump, 2026-09-14, then flat
    _add_security(world, 11, old)
    with world.run() as ctx:
        _placeholder_event(ctx, 11, "2026-10-29")                          # 45 days after the jump
    conn = connect(world.cfg.paths.db, readonly=True)
    built = [b for b in world.build_all(conn) if b.anomaly.security_id == 11]
    by_kind = {b.anomaly.kind: b for b in built}
    jump = by_kind["suspected_jump"]
    assert jump.anomaly.ex_date == "2026-09-14"
    assert jump.features.placeholder is False and jump.decision.rule_id != "R1"
    assert jump.pack["features"]["placeholder"] is False
    # the placeholder is not swallowed by the old jump either: it is its own anomaly
    ph = by_kind["index_placeholder"]
    assert (ph.anomaly.isin, ph.anomaly.ex_date, ph.decision.rule_id) == ("INE000000011", "2026-10-29", "R1")


def test_a_placeholder_inside_the_window_still_folds_into_the_jump(world):
    old = {d: (100.0 if d < "2026-10-14" else 50.0) for d in BDAYS}        # jump 2026-10-14, event 15 days later
    _add_security(world, 11, old)
    with world.run() as ctx:
        _placeholder_event(ctx, 11, "2026-10-29")
    conn = connect(world.cfg.paths.db, readonly=True)
    built = [b for b in world.build_all(conn) if b.anomaly.security_id == 11]
    assert [b.anomaly.kind for b in built] == ["suspected_jump"]
    assert built[0].features.placeholder is True and built[0].decision.rule_id == "R1"


# ------------------------------------------------------------------ R-8: pack.build_one + check_schema for each non-trivial rule

def _set_series(world, sid, series):
    conn = connect(world.cfg.paths.db)
    conn.execute("INSERT INTO universe_membership (as_of, observed_at, security_id, index_name, nse_symbol, nse_sector, "
                 "series, source) VALUES ('2026-10-20', '2026-10-20T10:00:00.000000Z', ?, 'NIFTY500', ?, ?, ?, 'nse_csv')",
                 (sid, f"SYM{sid}", "S", series))
    conn.close()


def _assert_valid(b):
    assert check_schema(b.proposal, producer_kind="engine") == []
    assert len(b.pack_raw) <= 12 * 1024 and len(json.dumps(b.proposal)) <= 8 * 1024
    assert b.proposal["producer"]["pack_sha256"] == b.pack_sha256
    cited = {c["id"] for c in b.proposal["citations"]}
    for claim in b.proposal["claims"]:
        assert all(r.startswith("f:") or r in cited for r in claim["citations"])
    return b.proposal


def test_pack_r1_series_not_eq_waits_with_no_factor(world):
    _set_series(world, 1, "BE")
    b = world.build()
    p = _assert_valid(b)
    assert b.features.series == "BE"
    assert (p["classification"]["label"], p["classification"]["rule_id"], p["classification"]["subtype"],
            p["classification"]["confidence"]) == ("pending_listing", "R1", "series_not_eq", "medium")
    r = p["resolution"]
    assert (r["action"], r["resolve_kind"], r["factor"], r["factor_basis"], r["recheck_on"]) == (
        "wait", None, None, None, "2026-11-06")
    assert p["claims"][0]["text"].startswith("The security trades in series BE")


def test_pack_r2_exchange_spos_gives_a_cited_exchange_derived_factor(world):
    reg = register(world, source_class="exchange", relevance="entity", entity_match=True, relevance_basis="synthetic",
                   title="Special pre-open session for the scheme", excerpt="SYNTHETIC circular excerpt",
                   typed_fields={"prev_close": 150.0, "spos_price": 90.0})
    b = world.build()
    p = _assert_valid(b)
    c, r = p["classification"], p["resolution"]
    assert (c["label"], c["rule_id"], c["confidence"]) == ("demerger", "R2", "medium")      # no body file: not high
    assert (r["action"], r["resolve_kind"], r["factor_basis"], r["needs_force"]) == ("resolve", "demerger", "exchange_spos", False)
    assert r["factor"] == pytest.approx(150.0 / 90.0, abs=1e-6)
    assert {(i["name"], i["value"]) for i in r["factor_inputs"]} == {("prev_close", 150.0), ("spos_price", 90.0)}
    assert [x["capture_id"] for x in p["citations"]] == [reg.capture_id]
    assert all(i["citation"] == "c1" for i in r["factor_inputs"])
    assert "market-implied factor" in c["alternatives"][0]["why_not"]
    assert b.evidence == [{"capture_id": reg.capture_id, "sha256": reg.sha256}]


def test_pack_r3_demerger_document_gives_a_market_implied_factor_with_its_band(world):
    reg = register(world, source_class="company", relevance="entity", entity_match=True, relevance_basis="synthetic",
                   title="Board approves demerger scheme", published_at="2026-09-15",
                   excerpt="SYNTHETIC board approves demerger scheme of arrangement")
    b = world.build()
    p = _assert_valid(b)
    c, r = p["classification"], p["resolution"]
    assert (c["label"], c["rule_id"], c["confidence"]) == ("demerger", "R3", "medium")
    assert (r["action"], r["resolve_kind"], r["factor_basis"], r["factor_uncertainty_pct"]) == (
        "resolve", "demerger", "market_implied", 12.0)
    assert r["factor"] == pytest.approx(1.0 / b.features.gross_factor, abs=1e-5) and r["factor_inputs"] == []
    assert [x["capture_id"] for x in p["citations"]] == [reg.capture_id]
    assert b.pack["features"]["vendor_split_rows"] == [] and abs(b.features.sector_peer_median_move_pct
                                                                  - b.features.market_median_move_pct) < 3.0


def test_pack_r4_vendor_split_on_another_date_is_a_flagged_misdated_adjustment(world):
    target = 1.0 / world.build().features.gross_factor
    reg = register(world, provenance="yahoo_actions", title="Yahoo corporate actions for SYM1.NS", published_at=None,
                   published_at_basis=None, source_class="other", relevance="context", entity_match=None,
                   relevance_basis="ticker_query_unverified", excerpt=None,
                   typed_fields={"ticker": "SYM1.NS", "window_days": 400, "n_splits": 1,
                                 "split_1_date": "2026-10-26", "split_1_ratio": round(target, 4),
                                 "splits_truncated": False, "n_dividends_pm5d": 0})
    b = world.build()
    p = _assert_valid(b)
    c, r = p["classification"], p["resolution"]
    assert (c["label"], c["rule_id"], c["subtype"], c["confidence"]) == ("vendor_error_other", "R4", "misdated_adjustment", "medium")
    assert (r["action"], r["resolve_kind"], r["factor_basis"], r["needs_primary"]) == ("resolve", "split", "vendor_ratio", True)
    assert r["factor"] == pytest.approx(round(target, 4), abs=1e-6)
    assert [i["name"] for i in r["factor_inputs"]] == ["ratio"] and r["factor_inputs"][0]["citation"] == "c1"
    assert [x["capture_id"] for x in p["citations"]] == [reg.capture_id]
    assert b.pack["features"]["vendor_split_rows"][0]["date"] == "2026-10-26"
    assert b.pack["source_status"]["yahoo_actions"] == "ok"


def test_pack_r5_cross_section_gives_a_round_ratio_or_no_factor_and_waits_for_later_bars(world):
    step = {d: (100.0 if d < EX else 50.0) for d in BDAYS}
    for sid in (11, 12, 13):
        _add_security(world, sid, step)                       # three more securities halve on the same quiet day
    conn = connect(world.cfg.paths.db, readonly=True)
    built = {b.anomaly.security_id: b for b in world.build_all(conn)}
    assert {1, 11, 12, 13} <= set(built)
    halved = built[11]
    p = _assert_valid(halved)
    c, r = p["classification"], p["resolution"]
    assert (c["label"], c["rule_id"], c["subtype"], c["confidence"]) == ("vendor_error_other", "R5", "misdated_adjustment", "low")
    assert (r["action"], r["resolve_kind"], r["factor"], r["factor_basis"], r["needs_primary"]) == (
        "resolve", "split", 2.0, "round_ratio", True)
    assert r["recheck_on"] == "2026-11-02"                    # a later reversal bar could overturn a price-only call
    assert halved.pack["features"]["cross_section_count"] == 4 and abs(halved.features.market_median_move_pct) < 2.0
    assert p["citations"] == [] and all(cl["citations"] for cl in p["claims"] if cl["type"] == "fact")
    # the same date's 0.6 jump is also a cross-section hit but has no round ratio: R5 makes no call on the factor
    q = _assert_valid(built[1])
    assert (q["classification"]["rule_id"], q["resolution"]["action"], q["resolution"]["resolve_kind"],
            q["resolution"]["factor"], q["resolution"]["factor_basis"]) == ("R5", "none", None, None, None)


def test_pack_r8_market_stress_is_genuine_at_low_confidence_with_a_recheck(world):
    drop = {d: (100.0 if d < EX else 94.0) for d in BDAYS}
    for sid in range(11, 19):
        _add_security(world, sid, drop)                       # eight more securities fall 6%: the market median is -6%
    conn = connect(world.cfg.paths.db, readonly=True)
    b = next(x for x in world.build_all(conn) if x.anomaly.security_id == 1)
    p = _assert_valid(b)
    c, r = p["classification"], p["resolution"]
    assert (c["label"], c["rule_id"], c["subtype"], c["confidence"]) == ("genuine_event", "R8", "market_stress", "low")
    assert (r["action"], r["resolve_kind"], r["factor"], r["factor_basis"], r["recheck_on"]) == (
        "resolve", "genuine", 1.0, None, "2026-11-02")
    assert b.features.market_median_move_pct <= -4.0 and p["citations"] == []
    assert any(cl["text"].startswith("The market median moved") for cl in p["claims"])


# ------------------------------------------------------------------ adversarial review fixes (F1, F2, F3, GOV-1)

def _news(**over):
    base = dict(source_class="news", provenance="yahoo_news", relevance="entity", entity_match=True,
                published_at="2026-10-26", published_at_basis="source")
    base.update(over)
    return item(**base)


def _agent_sector_doc(**over):
    base = dict(provenance="agent_attach", published_at_basis="agent_declared", source_class="regulator",
                relevance="sector", entity_match=None, published_at="2026-10-26")
    base.update(over)
    return item(**base)


_DOCS = {
    "news": lambda: _news(),
    "agent_attach": lambda: _agent_sector_doc(),
    "human_regulator": lambda: sector_doc(published_at="2026-10-26"),
}


@pytest.mark.parametrize("doc", sorted(_DOCS))
def test_a_dated_document_cannot_outrank_a_same_date_cross_section_artifact(doc):
    """F1 and GOV-1: five jumps on one quiet day are a vendor artifact whatever headline or attached link exists."""
    f = F(gross_factor=0.251705, cross_section_count=5, market_median_move_pct=0.0, sector_peer_median_move_pct=-5.0,
          n_peers_moved=3)
    without = run(f)
    with_doc = run(f, _DOCS[doc]())
    assert (without.label, without.rule_id) == ("vendor_error_other", "R5")
    assert (with_doc.label, with_doc.rule_id, with_doc.confidence) == ("vendor_error_other", "R5", "low")
    assert with_doc.resolve_kind == without.resolve_kind and with_doc.factor == without.factor


@pytest.mark.parametrize("jumps", [((-2, "2026-10-26", 2.04),), ((1, "2026-10-29", 2.04),)], ids=["prior", "forward"])
@pytest.mark.parametrize("doc", sorted(_DOCS))
def test_a_dated_document_cannot_outrank_a_reversal_glitch(doc, jumps):
    f = F(gross_factor=0.494, nearby_jumps=jumps, n_peers_moved=3, sector_peer_median_move_pct=-5.0)
    assert run(f).rule_id == "R6"
    d = run(f, _DOCS[doc]())
    assert (d.label, d.rule_id, d.subtype, d.confidence, d.action) == ("vendor_error_other", "R6", "one_bar_glitch", "low", "none")


@pytest.mark.parametrize("over", [
    {"cross_section_count": 2},                          # fewer than the R5 minimum
    {"cross_section_count": 5, "market_median_move_pct": 2.0},      # the market moved: not a quiet-day artifact
    {"cross_section_count": 5, "market_median_move_pct": -3.0},
])
def test_r7_still_fires_when_the_cross_section_is_small_or_the_market_moved(over):
    f = r7_features(**over)
    assert run(f, sector_doc(published_at="2026-10-26")).rule_id == "R7"


def test_r7_does_not_need_later_bars_the_forward_reversal_is_unknowable_on_day_0():
    f = r7_features(n_post_bars=0)
    assert f.post_bars_available is False
    assert run(f, sector_doc(published_at="2026-10-26")).rule_id == "R7"


def test_r7_with_an_unknown_cross_section_or_reversal_is_not_clean():
    assert run(r7_features(cross_section_count=None), sector_doc(published_at="2026-10-26")).rule_id != "R7"
    assert run(r7_features(cross_section_count=5, market_median_move_pct=None),
               sector_doc(published_at="2026-10-26")).rule_id != "R7"
    assert run(r7_features(gross_factor=None), sector_doc(published_at="2026-10-26")).rule_id != "R7"


def test_r8_market_stress_ignores_the_new_vendor_artifact_checks():
    """A reversal inside a crash is still a market-stress call (low, no resolve command): R8's blockers did not change."""
    f = F(gross_factor=0.8, market_median_move_pct=-4.4, nearby_jumps=((-2, "2026-10-26", 1.25),))
    assert run(f).rule_id == "R8"


_ACTION_TITLES = [
    "TESTCO announces bonus issue 1:1",
    "TESTCO board approves sub-division of equity shares (stock split) 1:2",
    "TESTCO board approves subdivision of shares",
    "TESTCO rights issue of equity shares",
    "TESTCO fixes entitlement ratio",
    "TESTCO shares trade ex-bonus from today",
    "TESTCO shares trade ex-split from today",
    "TESTCO announces a reverse stock split",
]


@pytest.mark.parametrize("title", _ACTION_TITLES)
def test_a_split_bonus_or_rights_document_blocks_r7_and_never_makes_a_demerger(title):
    """F2: the missed-split class is a corporate action. The engine fails safe to unknown, and R3 stays demerger-only."""
    f = F(gross_factor=0.5, n_peers_moved=0, market_median_move_pct=0.0, sector_peer_median_move_pct=0.0)
    d = run(f, item(title=title, excerpt=None, published_at="2026-10-25"))
    assert d.rule_id != "R7" and d.label == "unknown", (d.label, d.rule_id)
    assert d.missing_evidence[0].startswith("resolve the counter-evidence")


@pytest.mark.parametrize("title", ["Regulator consultation on copyrights and brightness", "TESTCO outlines the bonuses policy"])
def test_the_action_terms_match_whole_words_only(title):
    f = r7_features()
    assert run(f, sector_doc(title=title, excerpt=None, published_at="2026-10-26")).rule_id == "R7"


@pytest.mark.parametrize("action", ["bonus", "split", "rights", "demerger", "scheme"])
def test_a_typed_action_type_other_than_genuine_blocks_r7(action):
    doc = sector_doc(published_at="2026-10-26", typed_fields={"action_type": action})
    d = run(r7_features(), doc)
    assert d.rule_id != "R7" and d.label == "unknown"
    assert run(r7_features(), sector_doc(published_at="2026-10-26", typed_fields={"action_type": "genuine"})).rule_id == "R7"


_DEMERGER_BODY = "TESTCO Limited. The Board approved a Scheme of Arrangement for the demerger of the CV undertaking; record date to be fixed."


def _body_doc(tmp_path, text, *, store=True, **over):
    data = text.encode("utf-8")
    sha = evidence.store_body(tmp_path, data) if store else __import__("hashlib").sha256(data).hexdigest()
    base = dict(title="TESTCO: stock exchange intimation dated 27 Oct 2026", excerpt=None, body_sha256=sha,
                source_class="company", relevance="entity", published_at="2026-10-25")
    base.update(over)
    return item(**base), sha


def test_the_private_body_is_scanned_for_demerger_wording(tmp_path):
    """F3: the body announces the demerger and the title is neutral. The engine must not call it genuine."""
    f = r7_features()
    doc, _ = _body_doc(tmp_path, _DEMERGER_BODY)
    d = run(f, doc, archive_dir=tmp_path)
    assert d.rule_id != "R7" and d.label == "unknown"
    assert d.missing_evidence[0].startswith("resolve the counter-evidence")
    clean, _ = _body_doc(tmp_path, "TESTCO Limited. Quarterly results and a note on the CV industry slowdown.")
    assert run(f, clean, archive_dir=tmp_path).rule_id == "R7"


@pytest.mark.parametrize("how", ["missing", "tampered", "undecodable", "no_archive_dir"])
def test_an_unreadable_body_makes_the_keyword_check_unknown_not_none_found(tmp_path, how):
    f = r7_features()
    if how == "missing":
        doc, _ = _body_doc(tmp_path, _DEMERGER_BODY, store=False)
        archive = tmp_path
    elif how == "tampered":
        doc, sha = _body_doc(tmp_path, "TESTCO quarterly results")
        (tmp_path / "triage_private" / sha).write_bytes(b"something else")
        archive = tmp_path
    elif how == "undecodable":
        raw = b"%PDF-1.4 \xff\xfe\x00 binary"
        sha = evidence.store_body(tmp_path, raw)
        doc = item(title="TESTCO: stock exchange intimation", excerpt=None, body_sha256=sha, source_class="company",
                   relevance="entity", published_at="2026-10-25")
        archive = tmp_path
    else:
        doc, _ = _body_doc(tmp_path, "TESTCO quarterly results")
        archive = None
    d = run(f, doc, archive_dir=archive)
    assert d.rule_id != "R7" and d.label == "unknown"
    assert any("unreadable evidence" in m and doc.capture_id in m for m in d.missing_evidence)


def test_r3_reads_the_body_for_the_demerger_keyword(tmp_path):
    f = F(gross_factor=0.6)
    doc, _ = _body_doc(tmp_path, _DEMERGER_BODY)
    d = run(f, doc, archive_dir=tmp_path)
    assert d.rule_id == "R3" and d.label == "demerger" and d.confidence == "medium"
    other, _ = _body_doc(tmp_path, "TESTCO quarterly results")
    assert run(f, other, archive_dir=tmp_path).rule_id != "R3"


def test_the_body_scan_is_bounded(tmp_path):
    text = ("x " * 260_000) + "demerger"                     # the keyword sits past the 500,000-character bound
    doc, _ = _body_doc(tmp_path, text)
    assert run(r7_features(), doc, archive_dir=tmp_path).rule_id == "R7"
