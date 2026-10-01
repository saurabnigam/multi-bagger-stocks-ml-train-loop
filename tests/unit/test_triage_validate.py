"""Agent-proposal validator and confidence grading (WP2, design section 7 rules 1-10).

Two fixture subjects under one cohort (as_of 2026-10-30, cutoff 2026-10-30T18:29:59.999999Z):
  A  INE155A01022@2026-10-14  a demerger look-alike (exchange circular with prev_close and SPOS price)
  B  INE417T01026@2026-10-23  a regulator paper that names no company (sector relevance only)
Every document below is synthetic. Nothing here is a real filing, and nothing touches the network.
"""
from __future__ import annotations

import copy
from pathlib import Path
import shutil

import pytest

from quant.config import load as load_config
from quant.db.core import apply_schema, connect
from quant.errors import Refused
from quant.run import RunContext
from quant.triage import evidence, store, validate
from quant.types import Actor, FrozenClock

AS_OF = "2026-10-30"
NOON = "2026-10-30T06:30:00.000000Z"
AFTER = "2026-10-30T18:30:00.000000Z"
ISIN_A, EX_A = "INE155A01022", "2026-10-14"
ISIN_B, EX_B = "INE417T01026", "2026-10-23"
KEY_A, KEY_B = f"{ISIN_A}@{EX_A}", f"{ISIN_B}@{EX_B}"
SUBJ_A = {"isin": ISIN_A, "ex_date": EX_A}
SUBJ_B = {"isin": ISIN_B, "ex_date": EX_B}

Q_SPOS = "Previous close Rs 660.75. SPOS price Rs 400.00"
EXCERPT_SPOS = "Previous close Rs 660.75. SPOS price Rs 400.00 for the demerger special pre-open session."
BODY_SPOS = ("Annexure. Previous close Rs 660.75. SPOS price Rs 400.00. "
             "The ratio of the demerger is one share for one share. Record date is 14 October.")
Q_BODY = "The ratio of the demerger is one share for one share"
Q_IRDAI = "Comments are invited until 25 October 2026"
EXCERPT_IRDAI = ("Comments are invited until 25 October 2026 on recalibrating the economics "
                 "of insurance distribution.")


def base_features(**over):
    f = {"gross_factor": 0.5985, "volume_ratio_vs_20d_median": 3.1, "sector_peer_median_move_pct": -1.2,
         "market_median_move_pct": -0.3, "n_peers_moved": 0, "vendor_split_rows": [],
         "cross_section_count": 1, "reversal_prior": False, "reversal_forward": None, "placeholder": False,
         "series": "EQ", "peer_set_pit": False, "post_bars_available": False}
    f.update(over)
    return f


def pack(features=None, evidence_rows=None, **over):
    p = {"schema": "triage.pack/1", "as_of": AS_OF, "cutoff_utc": "2026-10-30T18:29:59.999999Z",
         "features": features or base_features(), "evidence": evidence_rows or []}
    p.update(over)
    return p


@pytest.fixture(autouse=True)
def _fast_run_context(monkeypatch):
    """Every RunContext hashes the whole package and shells out to git; neither matters here (amendment F7)."""
    import quant.db.core as dbcore
    monkeypatch.setattr(dbcore, "code_sha256", lambda root=None: "0" * 64)
    monkeypatch.setattr(RunContext, "_get_git_sha", staticmethod(lambda: "test"))


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    root = tmp_path_factory.mktemp("triage_validate")
    db = root / "state.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load_config().with_paths(db=db, prices_db=root / "p.db", data_dir=root / "d",
                                   archive_dir=root / "a", knowledge_dir=root / "k", ui_dir=root / "u")
    arch = cfg.paths.archive_dir
    ids: dict[str, str] = {}

    def env(**over):
        base = dict(provenance="human_attach", subject=SUBJ_A, url="https://example.test/x", title="t",
                    published_at="2026-10-10T04:00:00Z", published_at_basis="source", source_class="exchange",
                    relevance="entity", relevance_basis="token_match", excerpt=None, entity_match=True)
        base.update(over)
        return base

    body = evidence.store_body(arch, BODY_SPOS.encode("utf-8"))
    ctx = RunContext(as_of=AS_OF, kind="triage", track="live", cfg=cfg, clock=FrozenClock(NOON),
                     actor=Actor(kind="system", name="seed"))
    with ctx:
        reg = lambda name, **o: ids.__setitem__(name, evidence.register(ctx, env(**o)).capture_id)  # noqa: E731
        reg("spos_body", title="NSE circular: demerger and special pre-open session", excerpt=EXCERPT_SPOS,
            typed_fields={"prev_close": 660.75, "spos_price": 400.0, "action_type": "demerger"}, body_sha256=body)
        reg("spos_url", title="NSE circular (link only)", excerpt=EXCERPT_SPOS,
            typed_fields={"prev_close": 660.75, "spos_price": 400.0, "action_type": "demerger"})
        reg("company_demerger", title="Company filing: scheme of arrangement for demerger", source_class="company",
            excerpt="The board approved the demerger. Shareholders of record receive shares in the new company.")
        reg("news_a", title="Shares slide after restructuring news", source_class="news",
            headline="Stock falls 40 percent on ex-date as demerger takes effect", provenance="yahoo_news",
            excerpt=None)
        reg("undated_a", title="Undated circular", published_at=None, published_at_basis=None, excerpt=EXCERPT_SPOS)
        reg("badent_a", title="Circular that may concern another firm", entity_match=False, excerpt=EXCERPT_SPOS)
        reg("nullent_a", title="Circular not token-checked", entity_match=None, excerpt=EXCERPT_SPOS)
        reg("skew_a", title="Circular dated after we saw it", published_at="2026-10-30T10:00:00Z",
            excerpt=EXCERPT_SPOS)
        reg("future_a", title="Circular dated tomorrow", published_at="2026-10-31", excerpt=EXCERPT_SPOS)
        reg("missing_body", title="Circular whose body file is gone", excerpt="Short excerpt only.",
            body_sha256="c" * 64)
        reg("irdai_b", subject=SUBJ_B, title="IRDAI consultation paper on insurance distribution economics",
            source_class="regulator", relevance="sector", relevance_basis="attacher_declared", entity_match=False,
            published_at="2026-10-21", published_at_basis="human_declared", excerpt=EXCERPT_IRDAI)
        # R7 document window is [ex-5d, ex] = [2026-10-18, 2026-10-23] for subject B
        for name, day in (("irdai_old", "2026-09-23"), ("irdai_edge_lo", "2026-10-18"),
                          ("irdai_edge_hi", "2026-10-23"), ("irdai_below", "2026-10-17"),
                          ("irdai_after", "2026-10-24")):
            reg(name, subject=SUBJ_B, title=f"IRDAI consultation paper ({name})", source_class="regulator",
                relevance="sector", relevance_basis="attacher_declared", entity_match=False, published_at=day,
                published_at_basis="human_declared", excerpt=EXCERPT_IRDAI)
        reg("news_b", subject=SUBJ_B, title="Policy platform shares drop", source_class="news",
            headline="Policybazaar parent falls after regulator paper", provenance="yahoo_news")
    ctx2 = RunContext(as_of=AS_OF, kind="triage", track="live", cfg=cfg, clock=FrozenClock(AFTER),
                      actor=Actor(kind="system", name="seed"))
    with ctx2:
        ids["late_a"] = evidence.register(ctx2, env(title="Canary circular captured after the cutoff",
                                                    excerpt=EXCERPT_SPOS)).capture_id
    return {"root": root, "cfg": cfg, "ids": ids, "body": body, "archive": arch}


_DEFAULT_PACK = object()      # pk=None means "no pack at all", which is a different case


class World:
    def __init__(self, seeded, tmp_path):
        self.ids = seeded["ids"]
        self.body = seeded["body"]
        shutil.copy(seeded["root"] / "state.db", tmp_path / "state.db")
        self.cfg = seeded["cfg"].with_paths(db=tmp_path / "state.db")
        self.archive = seeded["archive"]

    def validate(self, proposal, *, pk=_DEFAULT_PACK, engine=None, pack_sha256=None, readonly=False, archive=True):
        conn = connect(self.cfg.paths.db, readonly=readonly)
        try:
            return validate.validate(proposal, conn=conn, as_of=AS_OF, pack=pack() if pk is _DEFAULT_PACK else pk,
                                     engine_proposal=engine,
                                     pack_sha256=pack_sha256, archive_dir=self.archive if archive else None)
        finally:
            conn.close()

    def register(self, now=NOON, **fields):
        ctx = RunContext(as_of=AS_OF, kind="triage", track="live", cfg=self.cfg, clock=FrozenClock(now),
                         actor=Actor(kind="system", name="t"))
        with ctx:
            return evidence.register(ctx, fields)


@pytest.fixture
def w(seeded, tmp_path):
    return World(seeded, tmp_path)


def codes(res):
    return sorted({e.split(":", 1)[0] for e in res.errors})


def has(res, code, text=""):
    return any(e.startswith(code + ":") and text in e for e in res.errors)


def cit(cid, capture_id, quote):
    return {"id": cid, "capture_id": capture_id, "quote": quote}


def claim(cid, text, citations, typ="fact", depends_on=()):
    return {"id": cid, "type": typ, "text": text, "citations": list(citations), "depends_on": list(depends_on)}


def proposal(key, label, **over):
    p = {
        "schema": "triage.proposal/1", "key": key, "as_of": AS_OF,
        "producer": {"kind": "llm", "name": "llm:test-model", "prompt_sha256": "d" * 64,
                     "pack_sha256": "b" * 64, "self_confidence": "high"},
        "anomaly_kind": "suspected_jump",
        "classification": {"label": label, "subtype": None, "confidence": "high", "rule_id": "R2",
                           "alternatives": []},
        "resolution": {"action": "none", "resolve_kind": None, "factor": None, "factor_basis": None,
                       "factor_uncertainty_pct": None, "needs_force": False, "needs_primary": False,
                       "recheck_on": None, "factor_inputs": []},
        "claims": [], "citations": [], "negative_checks": [], "missing_evidence": [], "needs_human": [],
        "usage": {"n_fetches": 0, "tokens_in": 9000, "tokens_out": 900, "wall_s": 12.5},
    }
    for k, v in over.items():
        p[k] = v
    return p


def spos_demerger(w, doc="spos_body", **over):
    """Agent proposal for subject A: demerger, factor from the SPOS circular."""
    cid = w.ids[doc]
    p = proposal(KEY_A, "demerger")
    p["resolution"].update(action="resolve", resolve_kind="demerger", factor=None, factor_basis="exchange_spos",
                           factor_inputs=[{"name": "prev_close", "value": 660.75, "citation": "c1"},
                                          {"name": "spos_price", "value": 400.0, "citation": "c1"}])
    p["citations"] = [cit("c1", cid, Q_SPOS)]
    p["claims"] = [claim("k1", "The circular gives a previous close of 660.75 and an SPOS price of 400.00.", ["c1"]),
                   claim("k2", "So the value moved to the new company on the ex-date 2026-10-14.", ["c1"], "inference",
                         ["k1"])]
    for k, v in over.items():
        p[k] = v
    return p


def policy_genuine(w, **over):
    p = proposal(KEY_B, "genuine_event")
    p["classification"]["rule_id"] = "R7"
    p["resolution"].update(action="resolve", resolve_kind="genuine", factor=1.0, recheck_on="2026-11-25")
    p["citations"] = [cit("c1", w.ids["irdai_b"], Q_IRDAI)]
    p["claims"] = [claim("k1", "The regulator invited comments until 25 October 2026 on distribution economics.",
                         ["c1"])]
    for k, v in over.items():
        p[k] = v
    return p


def engine_b():
    """The engine's own row for subject B: it looked and matched no rule."""
    return _engine("unknown", key=KEY_B)


# ------------------------------------------------------------------ happy paths

def test_spos_proposal_passes_and_the_engine_derives_factor_confidence_and_rule(w):
    res = w.validate(spos_demerger(w))
    assert res.ok, res.errors
    n = res.proposal
    assert n["resolution"]["factor"] == pytest.approx(660.75 / 400.0, abs=1e-6)
    assert n["resolution"]["factor_basis"] == "exchange_spos" and n["resolution"]["needs_force"] is False
    assert n["classification"]["confidence"] == "high" and n["classification"]["rule_id"] == "R2"
    assert n["producer"]["self_confidence"] == "high"          # stored, ignored
    assert res.flags == [] and res.errors == []
    assert res.evidence == [{"capture_id": w.ids["spos_body"],
                             "sha256": evidence.get_capture(connect(w.cfg.paths.db), w.ids["spos_body"])["sha256"]}]


def test_a_validated_proposal_can_be_stored_under_an_llm_actor(w):
    res = w.validate(spos_demerger(w))
    ctx = RunContext(as_of=AS_OF, kind="triage", track="live", cfg=w.cfg, clock=FrozenClock(NOON),
                     actor=Actor(kind="llm", name="test-model"))
    with ctx:
        stored = store.write_proposal(ctx, res.proposal, res.evidence, flags=res.flags)
        row = store.get_proposal(ctx.conn, stored.proposal_id)
    assert row["proposed_by"] == "llm:test-model" and row["rule_id"] == "TRIAGE_AGENT_V1"
    assert len(row["evidence_json"].encode()) <= 2048 and len(row["payload_json"].encode()) <= 8192


def test_validation_works_on_a_read_only_connection(w):
    assert w.validate(spos_demerger(w), readonly=True).ok


def test_sector_regulator_paper_with_corroboration_is_medium(w):
    res = w.validate(policy_genuine(w), pk=pack(base_features(gross_factor=0.6415, n_peers_moved=3)), engine=engine_b())
    assert res.ok, res.errors
    assert res.proposal["classification"]["confidence"] == "medium"
    assert res.proposal["resolution"]["factor"] == 1.0 and res.proposal["resolution"]["factor_basis"] is None
    assert res.proposal["resolution"]["needs_force"] is False    # a genuine move is exempt from the band guard


def test_sector_regulator_paper_without_corroboration_is_low(w):
    res = w.validate(policy_genuine(w), pk=pack(base_features(gross_factor=0.6415, n_peers_moved=1)), engine=engine_b())
    assert res.ok, res.errors
    assert res.proposal["classification"]["confidence"] == "low"


# ------------------------------------------------------------------ rule 1: schema, enums, caps

def _mutate(fn):
    def apply(p):
        fn(p)
        return p
    return apply


SCHEMA_BREAKERS = {
    "missing key": lambda p: p.pop("resolution"),
    "extra top-level key": lambda p: p.__setitem__("surprise", 1),
    "wrong schema id": lambda p: p.__setitem__("schema", "triage.proposal/2"),
    "bad label": lambda p: p["classification"].__setitem__("label", "moonshot"),
    "bad confidence enum": lambda p: p["classification"].__setitem__("confidence", "certain"),
    "bad action": lambda p: p["resolution"].__setitem__("action", "apply"),
    "resolve without kind": lambda p: p["resolution"].update(action="resolve", resolve_kind=None),
    "kind without resolve": lambda p: p["resolution"].update(action="none", resolve_kind="genuine"),
    "bad basis": lambda p: p["resolution"].__setitem__("factor_basis", "vibes"),
    "negative factor": lambda p: p["resolution"].__setitem__("factor", -1.0),
    "string factor": lambda p: p["resolution"].__setitem__("factor", "1.5"),
    "nan factor": lambda p: p["resolution"].__setitem__("factor", float("nan")),
    "bad key shape": lambda p: p.__setitem__("key", "not-a-key"),
    "bad as_of": lambda p: p.__setitem__("as_of", "30-10-2026"),
    "as_of differs from cohort": lambda p: p.__setitem__("as_of", "2026-09-30"),
    "producer not llm": lambda p: p["producer"].update(name="rules:triage-rules/1"),
    "producer kind engine": lambda p: p["producer"].update(kind="engine"),
    "producer name without prefix": lambda p: p["producer"].update(name="test-model"),
    "bad pack hash": lambda p: p["producer"].update(pack_sha256="xyz"),
    "claim cites unknown id": lambda p: p["claims"].append(claim("k9", "x", ["c9"])),
    "fact claim without citation": lambda p: p["claims"].append(claim("k9", "Plain text.", [])),
    "claim depends on unknown": lambda p: p["claims"].append(claim("k9", "x", ["c1"], "inference", ["k77"])),
    "duplicate citation id": lambda p: p["citations"].append(dict(p["citations"][0])),
    "capture id shape": lambda p: p["citations"][0].update(capture_id="cap_123"),
    "bad claim type": lambda p: p["claims"][0].update(type="opinion"),
    "too many alternatives": lambda p: p["classification"].update(
        alternatives=[{"label": "unknown", "why_not": "x"}] * 6),
    "why_not over 200 chars": lambda p: p["classification"].update(
        alternatives=[{"label": "unknown", "why_not": "x" * 201}]),
    "bad factor input name": lambda p: p["resolution"]["factor_inputs"][0].update(name="mystery"),
    "factor input cites nothing": lambda p: p["resolution"]["factor_inputs"][0].update(citation="c9"),
    "bool where number expected": lambda p: p["usage"].update(wall_s=True),
    "bad recheck date": lambda p: p["resolution"].update(recheck_on="soon"),
}


@pytest.mark.parametrize("name", list(SCHEMA_BREAKERS))
def test_rule1_schema_failures_refuse_the_whole_proposal(w, name):
    p = spos_demerger(w)
    SCHEMA_BREAKERS[name](p)
    res = w.validate(p)
    assert not res.ok and res.proposal is None and res.evidence == [], name
    assert has(res, "V1") or has(res, "V4"), (name, res.errors)


def test_rule1_payload_over_8kb_is_refused(w):
    p = spos_demerger(w)
    p["missing_evidence"] = ["m" * 200] * 10
    p["needs_human"] = ["h" * 200] * 10
    p["claims"] += [claim(f"k{i}", "z" * 400, ["c1"], "inference", ["k1"]) for i in range(3, 12)]
    res = w.validate(p)
    assert not res.ok and has(res, "V1", "8192")


def test_rule1_pack_hash_binding_is_enforced_when_the_caller_supplies_it(w):
    p = spos_demerger(w)
    assert w.validate(p, pack_sha256="b" * 64).ok
    res = w.validate(p, pack_sha256="e" * 64)
    assert not res.ok and has(res, "V1", "pack_sha256")


def test_every_independent_error_is_reported_not_just_the_first(w):
    p = spos_demerger(w)
    p["citations"][0]["quote"] = "This sentence is not in the document"     # rule 4
    p["claims"][0]["text"] = "The circular gives a previous close of 999.99."  # rule 5
    res = w.validate(p)
    assert not res.ok and {"V4", "V5"} <= set(codes(res))


# ------------------------------------------------------------------ rule 2: captures exist, are pre-cutoff, intact

def test_rule2_unknown_capture_is_refused(w):
    p = spos_demerger(w)
    p["citations"][0]["capture_id"] = "TD-" + "0" * 16
    res = w.validate(p)
    assert not res.ok and has(res, "V2", "capture_missing")


def test_rule2_post_cutoff_canary_citation_fails(w):
    res = w.validate(spos_demerger(w, doc="late_a"))
    assert not res.ok and has(res, "V2", "captured_after_cutoff")
    unknown = proposal(KEY_A, "unknown")
    unknown["citations"] = [cit("c1", w.ids["late_a"], Q_SPOS)]
    unknown["claims"] = [claim("k1", "Something with no number.", ["c1"])]
    assert has(w.validate(unknown), "V2", "captured_after_cutoff")


# Canary for the row-wins rule: validate never reads pack["evidence"]. If a later change starts to,
# this test and the next one must fail.
def test_rule2_forged_pack_captured_at_is_ignored(w):
    forged = pack(evidence_rows=[{"capture_id": w.ids["late_a"], "captured_at": "2026-10-01T00:00:00.000000Z",
                                  "sha256": "0" * 64}])
    res = w.validate(spos_demerger(w, doc="late_a"), pk=forged)
    assert not res.ok and has(res, "V2", "captured_after_cutoff")


def test_rule2_a_forged_late_captured_at_cannot_reject_a_pre_cutoff_row(w):
    forged = pack(evidence_rows=[{"capture_id": w.ids["spos_body"], "captured_at": "2026-11-15T00:00:00.000000Z",
                                  "sha256": "0" * 64}])
    res = w.validate(spos_demerger(w), pk=forged)
    assert res.ok, res.errors
    assert res.evidence[0]["capture_id"] == w.ids["spos_body"]


def test_rule2_tampered_envelope_is_refused(w):
    reg = w.register(title="Circular that will be tampered with", source_class="exchange",
                     subject=SUBJ_A, provenance="human_attach", published_at="2026-10-10",
                     published_at_basis="source", relevance="entity", entity_match=True, excerpt=EXCERPT_SPOS)
    w.ids = dict(w.ids, tampered=reg.capture_id)
    Path(reg.archive_path).write_bytes(Path(reg.archive_path).read_bytes().replace(b"2026-10-10", b"2026-01-01"))
    res = w.validate(spos_demerger(w, doc="tampered"))
    assert not res.ok and has(res, "V2", "envelope_hash_mismatch")


def test_rule2_missing_envelope_file_is_refused(w):
    reg = w.register(title="Circular whose file will vanish", source_class="exchange", subject=SUBJ_A,
                     provenance="human_attach", published_at="2026-10-10", published_at_basis="source",
                     relevance="entity", entity_match=True, excerpt=EXCERPT_SPOS)
    Path(reg.archive_path).unlink()
    w.ids = dict(w.ids, gone=reg.capture_id)
    res = w.validate(spos_demerger(w, doc="gone"))
    assert not res.ok and has(res, "V2", "envelope_file_missing")


# ------------------------------------------------------------------ rule 3: published_at for decisive claims

def test_rule3_undated_evidence_cannot_back_a_non_unknown_label(w):
    res = w.validate(spos_demerger(w, doc="undated_a"))
    assert not res.ok and has(res, "V3", "published_at_missing")


def test_rule3_undated_evidence_is_fine_behind_an_unknown_label(w):
    p = proposal(KEY_A, "unknown")
    p["classification"]["rule_id"] = None
    p["citations"] = [cit("c1", w.ids["undated_a"], "Previous close Rs 660.75")]
    p["claims"] = [claim("k1", "An undated circular quotes a previous close of 660.75.", ["c1"])]
    p["missing_evidence"] = ["a dated primary document"]
    res = w.validate(p)
    assert res.ok, res.errors
    assert res.proposal["classification"]["confidence"] == "low"


def test_rule3_published_after_capture_is_refused(w):
    res = w.validate(spos_demerger(w, doc="skew_a"))
    assert not res.ok and has(res, "V3", "published_after_capture")


def test_rule3_published_after_the_as_of_date_is_refused(w):
    res = w.validate(spos_demerger(w, doc="future_a"))
    assert not res.ok and (has(res, "V3", "published_after_cutoff") or has(res, "V3", "published_after_capture"))


# ------------------------------------------------------------------ rule 4: quotes

def test_rule4_quote_must_be_a_substring_of_the_envelope(w):
    p = spos_demerger(w)
    p["citations"][0]["quote"] = "SPOS price Rs 401.00"
    res = w.validate(p)
    assert not res.ok and has(res, "V4", "c1")


def test_rule4_quote_is_whitespace_and_case_normalised(w):
    p = spos_demerger(w)
    p["citations"][0]["quote"] = "  PREVIOUS   close rs 660.75.\nSPOS price   Rs 400.00 "
    assert w.validate(p).ok


def test_rule4_quote_over_25_words_is_refused(w):
    p = spos_demerger(w)
    p["citations"][0]["quote"] = " ".join(["word"] * 26)
    res = w.validate(p)
    assert not res.ok and has(res, "V4", "25")


def test_rule4_quote_of_exactly_25_words_is_allowed(w):
    excerpt = " ".join(f"alpha{i}" for i in range(1, 27))            # 26 words, none numeric-looking
    reg = w.register(title="Circular with a long excerpt", source_class="exchange", subject=SUBJ_A,
                     provenance="human_attach", published_at="2026-10-10", published_at_basis="source",
                     relevance="entity", entity_match=True, excerpt=excerpt)
    w.ids = dict(w.ids, long=reg.capture_id)
    p = spos_demerger(w, doc="long")
    p["resolution"].update(factor_basis="market_implied", factor_inputs=[])
    p["claims"] = [claim("k1", "The circular has a long excerpt.", ["c1"])]
    p["citations"][0]["quote"] = " ".join(excerpt.split()[:25])
    assert not has(w.validate(p), "V4")
    p["citations"][0]["quote"] = excerpt
    res = w.validate(p)
    assert not res.ok and has(res, "V4", "25")


def test_rule4_empty_quote_is_refused(w):
    p = spos_demerger(w)
    p["citations"][0]["quote"] = "   "
    res = w.validate(p)
    assert not res.ok and (has(res, "V4") or has(res, "V1"))


def test_rule4_quote_may_come_from_the_title_or_headline(w):
    p = spos_demerger(w)
    p["citations"][0]["quote"] = "NSE circular: demerger and special pre-open session"
    p["claims"] = [claim("k1", "The circular is about a demerger and a special pre-open session.", ["c1"])]
    p["resolution"].update(factor_basis="market_implied", factor_inputs=[])
    assert w.validate(p).ok
    n = proposal(KEY_A, "genuine_event")
    n["citations"] = [cit("c1", w.ids["news_a"], "Stock falls 40 percent on ex-date")]
    n["claims"] = [claim("k1", "A headline says the stock fell 40 percent on the ex-date.", ["c1"])]
    res = w.validate(n)
    assert not has(res, "V4")


def test_rule4_quote_from_the_private_body_is_checked_when_the_body_exists(w):
    p = spos_demerger(w)
    p["citations"].append(cit("c2", w.ids["spos_body"], Q_BODY))
    p["claims"].append(claim("k3", "The body says the ratio of the demerger is one share for one share.", ["c2"]))
    res = w.validate(p)
    assert res.ok, res.errors
    assert not any("body_unavailable" in n for n in res.notes)


def test_rule4_body_only_quote_fails_when_the_body_is_unavailable_and_is_recorded(w):
    p = spos_demerger(w)
    p["citations"].append(cit("c2", w.ids["spos_body"], Q_BODY))
    p["claims"].append(claim("k3", "The body says the ratio of the demerger is one share for one share.", ["c2"]))
    res = w.validate(p, archive=False)             # no archive directory: the body cannot be read
    assert not res.ok and has(res, "V4", "c2")
    assert any("body_unavailable" in n for n in res.notes)


def test_rule4_missing_body_file_is_recorded_and_excerpt_quotes_still_pass(w):
    p = spos_demerger(w, doc="missing_body")
    p["citations"][0]["quote"] = "Short excerpt only"
    p["resolution"]["factor_inputs"] = []
    p["resolution"].update(factor_basis="market_implied")
    p["claims"][0]["text"] = "The circular has a short excerpt."
    res = w.validate(p)
    assert any("body_unavailable" in n and w.ids["missing_body"] in n for n in res.notes)
    assert not has(res, "V4")


def test_rule4_tampered_body_is_refused(w):
    body_sha = evidence.store_body(w.archive, b"Tamper test body text for this one test.")
    reg = w.register(title="Circular with a body that will change", source_class="exchange", subject=SUBJ_A,
                     provenance="human_attach", published_at="2026-10-10", published_at_basis="source",
                     relevance="entity", entity_match=True, excerpt="Short excerpt", body_sha256=body_sha)
    (w.archive / "triage_private" / body_sha).write_bytes(b"Something else entirely.")
    w.ids = dict(w.ids, bodytamper=reg.capture_id)
    p = spos_demerger(w, doc="bodytamper")
    p["citations"][0]["quote"] = "Short excerpt"
    p["resolution"].update(factor_basis="market_implied", factor_inputs=[])
    p["claims"] = [claim("k1", "There is a short excerpt.", ["c1"])]
    res = w.validate(p)
    assert not res.ok and has(res, "V4", "body_tampered")


# ------------------------------------------------------------------ rule 5: numbers

def test_rule5_number_absent_from_quotes_and_features_is_refused(w):
    p = spos_demerger(w)
    p["claims"][0]["text"] = "The circular gives a previous close of 660.75 and an SPOS price of 401.00."
    res = w.validate(p)
    assert not res.ok and has(res, "V5", "401")


def test_rule5_number_formats_are_compared_numerically(w):
    p = spos_demerger(w)
    p["claims"][0]["text"] = "Previous close 660.750 and SPOS price 400."
    assert w.validate(p).ok
    n = proposal(KEY_B, "unknown")
    n["classification"]["rule_id"] = None
    n["citations"] = [cit("c1", w.ids["irdai_b"], Q_IRDAI)]
    n["claims"] = [claim("k1", "Comments run to 25 October 2026.", ["c1"])]
    assert w.validate(n).ok


def test_rule5_a_derived_number_is_refused_because_the_engine_computes_it(w):
    p = spos_demerger(w)
    p["claims"][1]["text"] = "The factor is 660.75 / 400.00 = 1.6519."
    res = w.validate(p)
    assert not res.ok and has(res, "V5", "1.6519")


def test_rule5_number_may_come_from_a_pack_feature_cited_as_f_name(w):
    p = spos_demerger(w)
    p["claims"].append(claim("k3", "The gross factor on the jump day was 0.5985.", ["f:gross_factor"]))
    assert w.validate(p).ok
    p["claims"][-1]["text"] = "The gross factor on the jump day was 0.61."
    assert not w.validate(p).ok                       # 0.5985 rounds to 0.60, not 0.61


def test_rule5_feature_rounding_to_the_digits_written_is_a_match(w):
    p = spos_demerger(w)
    p["claims"].append(claim("k3", "The gross factor was 0.60.", ["f:gross_factor"]))
    assert w.validate(p).ok


def test_rule5_an_integer_written_for_a_non_integer_feature_is_not_a_match(w):
    # 0.5985 rounds to 1 at zero decimals, but "1" is not a statement of 0.5985
    p = spos_demerger(w)
    p["claims"].append(claim("k3", "The gross factor was 1.", ["f:gross_factor"]))
    res = w.validate(p)
    assert not res.ok and has(res, "V5", "number_unsupported")
    q = spos_demerger(w)
    q["claims"].append(claim("k3", "The volume ratio was 3.", ["f:volume_ratio_vs_20d_median"]))
    assert has(w.validate(q), "V5", "number_unsupported")         # 3.1 is not 3


def test_rule5_an_integer_feature_matches_the_integer_written(w):
    p = spos_demerger(w)
    p["claims"].append(claim("k3", "Peers that moved in the same direction: 3.", ["f:n_peers_moved"]))
    assert w.validate(p, pk=pack(base_features(n_peers_moved=3))).ok


def test_rule5_unknown_feature_reference_is_refused(w):
    p = spos_demerger(w)
    p["claims"].append(claim("k3", "The gross factor was 0.5985.", ["f:no_such_feature"]))
    res = w.validate(p)
    assert not res.ok and has(res, "V5", "no_such_feature")


def test_rule5_inference_inherits_the_citations_of_what_it_depends_on(w):
    p = spos_demerger(w)
    p["claims"][1]["text"] = "With a previous close of 660.75 the drop is explained."
    p["claims"][1]["citations"] = []
    assert w.validate(p).ok                            # 660.75 is in k1's quote
    p["claims"][1]["depends_on"] = []
    assert not w.validate(p).ok


def test_rule5_the_cohort_dates_are_not_numbers_to_verify(w):
    p = spos_demerger(w)
    p["claims"][1]["text"] = f"The ex-date is {EX_A} and the cohort is {AS_OF}."
    assert w.validate(p).ok
    p["claims"][1]["text"] = "The ex-date is 2026-10-15."
    assert not w.validate(p).ok


def test_rule5_letters_glued_to_digits_are_not_numbers(w):
    p = spos_demerger(w)
    p["claims"][1]["text"] = "This is a Q3 FY27 matter for H2."
    assert w.validate(p).ok


# ------------------------------------------------------------------ rule 6: the engine recomputes the factor

def test_rule6_agent_factor_within_0_05_percent_is_accepted_and_replaced(w):
    exact = 660.75 / 400.0
    p = spos_demerger(w)
    p["resolution"]["factor"] = exact * (1 + 0.0004)
    res = w.validate(p)
    assert res.ok, res.errors
    assert res.proposal["resolution"]["factor"] == pytest.approx(exact, abs=1e-6)


def test_rule6_agent_factor_above_0_05_percent_is_refused(w):
    p = spos_demerger(w)
    p["resolution"]["factor"] = (660.75 / 400.0) * (1 + 0.0006)
    res = w.validate(p)
    assert not res.ok and has(res, "V6", "0.05%")


def test_rule6_factor_inputs_must_appear_in_their_cited_quote(w):
    p = spos_demerger(w)
    p["resolution"]["factor_inputs"][1]["value"] = 410.0
    res = w.validate(p)
    assert not res.ok and has(res, "V6", "spos_price")


def test_rule6_missing_input_for_the_basis_is_refused(w):
    p = spos_demerger(w)
    p["resolution"]["factor_inputs"] = p["resolution"]["factor_inputs"][:1]
    res = w.validate(p)
    assert not res.ok and has(res, "V6", "spos_price")


def test_rule6_resolve_without_a_basis_is_refused_for_value_transfer(w):
    p = spos_demerger(w)
    p["resolution"]["factor_basis"] = None
    p["resolution"]["factor_inputs"] = []
    res = w.validate(p)
    assert not res.ok and has(res, "V6", "factor_basis")


def test_rule6_market_implied_factor_is_one_over_gross_with_12_percent_uncertainty(w):
    p = spos_demerger(w, doc="company_demerger")
    p["resolution"].update(factor_basis="market_implied", factor_inputs=[], factor=None)
    p["citations"] = [cit("c1", w.ids["company_demerger"], "The board approved the demerger")]
    p["claims"] = [claim("k1", "The company filing says the board approved the demerger.", ["c1"])]
    res = w.validate(p)
    assert res.ok, res.errors
    assert res.proposal["resolution"]["factor"] == pytest.approx(1 / 0.5985, abs=1e-6)
    assert res.proposal["resolution"]["factor_uncertainty_pct"] == 12.0
    assert res.proposal["resolution"]["needs_force"] is False


def test_rule6_market_implied_needs_a_gross_factor_in_the_pack(w):
    p = spos_demerger(w, doc="company_demerger")
    p["resolution"].update(factor_basis="market_implied", factor_inputs=[], factor=None)
    p["citations"] = [cit("c1", w.ids["company_demerger"], "The board approved the demerger")]
    p["claims"] = [claim("k1", "The company filing says the board approved the demerger.", ["c1"])]
    res = w.validate(p, pk=pack(base_features(gross_factor=None)))
    assert not res.ok and has(res, "V6", "gross_factor")


def test_rule6_genuine_means_exactly_one(w):
    p = policy_genuine(w)
    p["resolution"]["factor"] = 1.3
    res = w.validate(p, pk=pack(base_features(gross_factor=0.6415)))
    assert not res.ok and has(res, "V6")
    p["resolution"]["factor"] = None
    res = w.validate(p, pk=pack(base_features(gross_factor=0.6415)), engine=engine_b())
    assert res.ok and res.proposal["resolution"]["factor"] == 1.0


def test_rule6_a_proposal_that_makes_no_resolve_call_carries_no_factor(w):
    p = proposal(KEY_A, "unknown")
    p["classification"]["rule_id"] = None
    p["resolution"]["factor"] = 1.5
    res = w.validate(p)
    assert not res.ok and has(res, "V6")


def test_rule6_needs_force_is_engine_derived_outside_the_band(w):
    # SPOS factor 1.6519 on a gross of 0.30 gives 0.496, outside [0.714, 1.4]
    p = spos_demerger(w)
    res = w.validate(p, pk=pack(base_features(gross_factor=0.30)))
    assert res.ok and res.proposal["resolution"]["needs_force"] is True
    p["resolution"]["needs_force"] = True            # the agent's own value is ignored both ways
    res = w.validate(p)
    assert res.ok and res.proposal["resolution"]["needs_force"] is False


# ------------------------------------------------------------------ rule 7: label requirements

def test_rule7_demerger_needs_a_cited_primary_source(w):
    p = spos_demerger(w)
    p["citations"] = [cit("c1", w.ids["news_a"], "Stock falls 40 percent on ex-date")]
    p["resolution"].update(factor_basis="market_implied", factor_inputs=[])
    p["claims"] = [claim("k1", "A headline says the stock fell 40 percent on ex-date.", ["c1"])]
    res = w.validate(p)
    assert not res.ok and has(res, "V7", "primary")


@pytest.mark.parametrize("label,kind", [("scheme", "scheme"), ("rights", "rights"),
                                        ("split_or_bonus_missed_by_vendor", "split")])
def test_rule7_scheme_rights_and_split_also_need_a_primary_source(w, label, kind):
    p = spos_demerger(w)
    p["classification"]["label"] = label
    p["resolution"].update(resolve_kind=kind, factor_basis="market_implied", factor_inputs=[])
    p["citations"] = [cit("c1", w.ids["news_a"], "Stock falls 40 percent on ex-date")]
    p["claims"] = [claim("k1", "A headline says the stock fell 40 percent on ex-date.", ["c1"])]
    res = w.validate(p)
    assert not res.ok and has(res, "V7", "primary")


def test_rule7_genuine_fails_when_the_engine_finds_a_demerger_document_the_agent_did_not_cite(w):
    p = proposal(KEY_A, "genuine_event")
    p["resolution"].update(action="resolve", resolve_kind="genuine", factor=1.0)
    p["citations"] = [cit("c1", w.ids["news_a"], "Stock falls 40 percent on ex-date")]
    p["claims"] = [claim("k1", "A headline says the stock fell 40 percent on ex-date.", ["c1"])]
    p["negative_checks"] = [{"check": "demerger_keyword_in_docs", "sources": [], "result": "none_found"}]
    res = w.validate(p)
    assert not res.ok and has(res, "V7", "negative_check")


def test_rule7_the_engine_negative_check_replaces_the_agents(w):
    p = policy_genuine(w)
    p["negative_checks"] = [{"check": "made_up", "sources": [], "result": "none_found"}]
    res = w.validate(p, pk=pack(base_features(gross_factor=0.6415)), engine=engine_b())
    assert res.ok, res.errors
    names = {c["check"] for c in res.proposal["negative_checks"]}
    assert "made_up" not in names and "demerger_keyword_in_docs" in names
    assert all(c["result"] == "none_found" for c in res.proposal["negative_checks"])


@pytest.mark.parametrize("features,why", [
    (base_features(gross_factor=0.6415, placeholder=True), "placeholder"),
    (base_features(gross_factor=0.6415, vendor_split_rows=[{"date": "2026-10-20", "split_ratio": 1.56}]),
     "vendor split"),
])
def test_rule7_genuine_fails_when_pack_shows_a_placeholder_or_vendor_split(w, features, why):
    res = w.validate(policy_genuine(w), pk=pack(features))
    assert not res.ok and has(res, "V7", "negative_check"), why


def test_rule7_genuine_fails_when_the_engine_already_matched_a_demerger_or_vendor_rule(w):
    for rule in ("R1", "R2", "R3", "R4"):
        engine = proposal(KEY_B, "unknown")
        engine["producer"] = {"kind": "engine", "name": "rules:triage-rules/1", "prompt_sha256": None,
                              "pack_sha256": "a" * 64, "self_confidence": None}
        engine["classification"].update(label="demerger", rule_id=rule, confidence="medium")
        res = w.validate(policy_genuine(w), pk=pack(base_features(gross_factor=0.6415)), engine=engine)
        assert not res.ok and has(res, "V7", "negative_check"), rule


def _vendor(w, **over):
    p = proposal(KEY_A, "vendor_error_other")
    p["classification"]["rule_id"] = "R4"
    p["resolution"].update(action="resolve", resolve_kind="manual_adj", factor_basis="vendor_ratio",
                           factor_inputs=[{"name": "ratio", "value": 1.67, "citation": "f:vendor_split_ratio"}])
    p["claims"] = [claim("k1", "The vendor lists a split ratio of 1.67 on another date than the jump.",
                         ["f:vendor_split_ratio"])]
    for k, v in over.items():
        p[k] = v
    return p


VENDOR_FEATURES = base_features(vendor_split_rows=[{"date": "2026-10-20", "split_ratio": 1.67}],
                                vendor_split_ratio=1.67)


def test_rule7_vendor_error_needs_a_vendor_conflict_feature(w):
    res = w.validate(_vendor(w), pk=pack(base_features(vendor_split_ratio=1.67)))
    assert not res.ok and has(res, "V7", "vendor")
    ok = w.validate(_vendor(w), pk=pack(VENDOR_FEATURES))
    assert ok.ok, ok.errors


CAND = {"date": "2026-06-04", "ratio": 1.67, "source": "yahoo_actions", "sessions_from_jump": 100, "compensating_jump": False}


def wide_features(cand, **over):
    return base_features(vendor_split_rows=[], vendor_split_candidates=[cand], vendor_split_ratio=1.67, **over)


def test_rule7_a_vendor_split_months_from_the_jump_is_a_vendor_conflict_feature_only_when_it_qualifies(w):
    """The same test the engine applies (D1): more than one session away, no compensating jump on its date."""
    assert w.validate(_vendor(w), pk=pack(wide_features(CAND))).ok
    for bad in (dict(CAND, sessions_from_jump=1), dict(CAND, sessions_from_jump=-1), dict(CAND, sessions_from_jump=0),
                dict(CAND, compensating_jump=True), dict(CAND, compensating_jump=None), dict(CAND, ratio=2.5)):
        res = w.validate(_vendor(w), pk=pack(wide_features(bad)))
        assert not res.ok and has(res, "V7", "vendor"), bad


def test_rule9_a_qualifying_far_split_grades_medium_a_compensated_one_low(w):
    res = w.validate(_vendor(w), pk=pack(wide_features(CAND)))
    assert res.ok and res.proposal["classification"]["confidence"] == "medium" and res.proposal["classification"]["rule_id"] == "R4"
    # a cross-section keeps the proposal valid (V7) but the far split does not qualify, so R4 is not derived
    res = w.validate(_vendor(w), pk=pack(wide_features(dict(CAND, compensating_jump=True), cross_section_count=4)))
    assert res.ok and res.proposal["classification"]["confidence"] == "low"


def test_rule9_a_pack_without_candidates_keeps_the_five_day_rule(w):
    res = w.validate(_vendor(w), pk=pack(VENDOR_FEATURES))
    assert res.ok and res.proposal["classification"]["confidence"] == "medium"


def test_rule7_a_cross_section_of_jumps_is_also_a_vendor_conflict_feature(w):
    feats = base_features(cross_section_count=4, vendor_split_ratio=1.67)
    assert w.validate(_vendor(w), pk=pack(feats)).ok


def test_rule7_label_and_resolve_kind_must_agree(w):
    p = spos_demerger(w)
    p["resolution"]["resolve_kind"] = "genuine"
    res = w.validate(p)
    assert not res.ok and has(res, "V7", "resolve_kind")
    for label in ("pending_listing", "unknown", "surveillance_or_series_change"):
        q = proposal(KEY_A, label)
        q["resolution"].update(action="resolve", resolve_kind="genuine", factor=1.0)
        assert has(w.validate(q), "V7", "resolve_kind"), label


# ------------------------------------------------------------------ rule 8: entity match

def test_rule8_entity_relevance_with_entity_match_false_cannot_support_a_claim(w):
    res = w.validate(spos_demerger(w, doc="badent_a"))
    assert not res.ok and has(res, "V8", "entity_match_false")


def test_rule8_entity_relevance_not_token_checked_cannot_support_a_claim(w):
    res = w.validate(spos_demerger(w, doc="nullent_a"))
    assert not res.ok and has(res, "V8", "entity_match_unverified")


def test_rule8_sector_relevance_may_back_a_claim_without_a_name_match(w):
    assert w.validate(policy_genuine(w), pk=pack(base_features(gross_factor=0.6415)), engine=engine_b()).ok


def test_rule8_evidence_attached_to_another_subject_cannot_be_cited(w):
    p = spos_demerger(w)
    p["citations"] = [cit("c1", w.ids["irdai_b"], Q_IRDAI)]
    p["claims"] = [claim("k1", "The regulator invited comments until 25 October 2026.", ["c1"])]
    p["resolution"].update(factor_basis="market_implied", factor_inputs=[])
    res = w.validate(p)
    assert not res.ok and has(res, "V8", "subject")


# ------------------------------------------------------------------ rule 9: confidence from grade()

def test_rule9_self_reported_confidence_is_ignored_in_both_directions(w):
    url_only = spos_demerger(w, doc="spos_url")
    url_only["producer"]["self_confidence"] = "high"
    url_only["classification"]["confidence"] = "high"
    res = w.validate(url_only)
    assert res.ok and res.proposal["classification"]["confidence"] == "medium"
    with_body = spos_demerger(w)
    with_body["producer"]["self_confidence"] = "low"
    with_body["classification"]["confidence"] = "low"
    res = w.validate(with_body)
    assert res.ok and res.proposal["classification"]["confidence"] == "high"
    assert res.proposal["producer"]["self_confidence"] == "low"


def test_rule9_r3_equivalent_is_medium_when_its_conditions_hold(w):
    p = spos_demerger(w, doc="company_demerger")
    p["resolution"].update(factor_basis="market_implied", factor_inputs=[], factor=None)
    p["citations"] = [cit("c1", w.ids["company_demerger"], "The board approved the demerger")]
    p["claims"] = [claim("k1", "The company filing says the board approved the demerger.", ["c1"])]
    res = w.validate(p)
    assert res.ok and res.proposal["classification"]["confidence"] == "medium"
    assert res.proposal["classification"]["rule_id"] == "R3"


def test_rule9_r3_equivalent_fails_safe_to_low_when_peers_explain_the_move(w):
    p = spos_demerger(w, doc="company_demerger")
    p["resolution"].update(factor_basis="market_implied", factor_inputs=[], factor=None)
    p["citations"] = [cit("c1", w.ids["company_demerger"], "The board approved the demerger")]
    p["claims"] = [claim("k1", "The company filing says the board approved the demerger.", ["c1"])]
    far = pack(base_features(sector_peer_median_move_pct=-6.0))
    res = w.validate(p, pk=far)
    assert res.ok and res.proposal["classification"]["confidence"] == "low"
    missing = pack(base_features(sector_peer_median_move_pct=None))
    assert w.validate(p, pk=missing).proposal["classification"]["confidence"] == "low"


def test_rule9_unknown_is_always_low(w):
    p = proposal(KEY_A, "unknown")
    p["classification"]["rule_id"] = None
    p["missing_evidence"] = ["a primary document"]
    res = w.validate(p)
    assert res.ok and res.proposal["classification"]["confidence"] == "low"
    assert res.proposal["classification"]["rule_id"] is None


def test_rule9_vendor_with_a_vendor_split_ratio_near_one_over_gross_is_medium_else_low(w):
    # gross 0.5985 -> 1/gross 1.6708; ratio 1.67 is within 3%
    res = w.validate(_vendor(w), pk=pack(VENDOR_FEATURES))
    assert res.ok and res.proposal["classification"]["confidence"] == "medium"
    assert res.proposal["resolution"]["needs_primary"] is True
    off = _vendor(w)
    off["resolution"]["factor_inputs"][0]["value"] = 2.5
    off["claims"][0]["text"] = "The vendor lists a split ratio of 2.5 on another date than the jump."
    feats = dict(VENDOR_FEATURES, vendor_split_ratio=2.5)
    res = w.validate(off, pk=pack(feats))
    assert res.ok and res.proposal["classification"]["confidence"] == "low"


# ------------------------------------------------------------------ rule 10: disagreement flag

def _engine(label, rule_id=None, key=KEY_A):
    e = proposal(key, label)
    e["producer"] = {"kind": "engine", "name": "rules:triage-rules/1", "prompt_sha256": None,
                     "pack_sha256": "a" * 64, "self_confidence": None}
    e["classification"].update(rule_id=rule_id, confidence="low")
    return e


def test_rule10_label_differing_from_the_engine_is_flagged(w):
    res = w.validate(spos_demerger(w), engine=_engine("unknown"))
    assert res.ok and res.flags == ["DISAGREES_WITH_ENGINE"]


def test_rule10_same_label_is_not_flagged(w):
    res = w.validate(spos_demerger(w), engine=_engine("demerger", "R2"))
    assert res.ok and res.flags == []


def test_rule10_without_an_engine_proposal_nothing_is_flagged_and_it_is_noted(w):
    res = w.validate(spos_demerger(w), engine=None)
    assert res.ok and res.flags == [] and any("engine_proposal_absent" in n for n in res.notes)


def test_rule10_the_flag_is_stored_on_the_proposal_row(w):
    res = w.validate(spos_demerger(w), engine=_engine("unknown"))
    ctx = RunContext(as_of=AS_OF, kind="triage", track="live", cfg=w.cfg, clock=FrozenClock(NOON),
                     actor=Actor(kind="llm", name="test-model"))
    with ctx:
        stored = store.write_proposal(ctx, res.proposal, res.evidence, flags=res.flags)
        assert store.get_proposal(ctx.conn, stored.proposal_id)["llm_review"] == "DISAGREES_WITH_ENGINE"


# ------------------------------------------------------------------ input is never mutated

def test_validate_does_not_mutate_the_submitted_proposal(w):
    p = spos_demerger(w)
    before = copy.deepcopy(p)
    w.validate(p)
    assert p == before


# ------------------------------------------------------------------ needs_primary follows R4/R5 only

def test_needs_primary_is_set_for_a_vendor_resolve_call_and_not_for_a_glitch(w):
    res = w.validate(_vendor(w), pk=pack(VENDOR_FEATURES))
    assert res.ok and res.proposal["resolution"]["needs_primary"] is True
    glitch = proposal(KEY_A, "vendor_error_other")
    glitch["classification"].update(rule_id="R6", subtype="one_bar_glitch")
    glitch["claims"] = []
    res = w.validate(glitch, pk=pack(base_features(reversal_prior=True)))
    assert res.ok, res.errors
    assert res.proposal["resolution"]["action"] == "none" and res.proposal["resolution"]["needs_primary"] is False


# ------------------------------------------------------------------ rule 7: negative check has three states

def _checks(res):
    return {c["check"]: c["result"] for c in res.proposal["negative_checks"]}


def test_negative_check_reports_none_found_only_when_every_input_was_present(w):
    res = w.validate(policy_genuine(w), pk=pack(base_features(gross_factor=0.6415)), engine=engine_b())
    assert res.ok, res.errors
    assert _checks(res) == {"demerger_keyword_in_docs": "none_found", "vendor_split_present": "none_found",
                            "placeholder_present": "none_found", "series_not_eq": "none_found",
                            "cross_section_jumps_present": "none_found", "reversal_pair_present": "none_found",
                            "engine_rule_r1_r4": "none_found"}


def test_negative_check_states_are_found_none_found_unknown():
    f = base_features()
    engine = _engine("unknown", key=KEY_B)
    got = {c["check"]: c["result"] for c in validate.negative_check(f, [], engine)}
    assert set(got.values()) == {"none_found"}
    # nothing known: every check that needs an input says unknown, never none_found
    got = {c["check"]: c["result"] for c in validate.negative_check({}, [], None)}
    assert got == {"demerger_keyword_in_docs": "none_found", "vendor_split_present": "unknown",
                   "placeholder_present": "unknown", "series_not_eq": "unknown",
                   "cross_section_jumps_present": "unknown", "reversal_pair_present": "unknown",
                   "engine_rule_r1_r4": "unknown"}
    # found beats everything
    found = {c["check"]: c["result"] for c in validate.negative_check(
        base_features(placeholder=True, series="BE", vendor_split_rows=[{"date": "2026-10-20"}],
                      cross_section_count=4, market_median_move_pct=0.1, reversal_prior=True), [],
        _engine("demerger", "R3", key=KEY_B))}
    assert found == {"demerger_keyword_in_docs": "none_found", "vendor_split_present": "found",
                     "placeholder_present": "found", "series_not_eq": "found",
                     "cross_section_jumps_present": "found", "reversal_pair_present": "found",
                     "engine_rule_r1_r4": "found"}


def test_an_empty_vendor_split_list_is_none_found_but_a_null_one_is_unknown():
    f = base_features(vendor_split_rows=None)
    assert {c["check"]: c["result"] for c in validate.negative_check(f, [], _engine("unknown"))}[
        "vendor_split_present"] == "unknown"
    f = base_features(vendor_split_rows=[])
    assert {c["check"]: c["result"] for c in validate.negative_check(f, [], _engine("unknown"))}[
        "vendor_split_present"] == "none_found"


def test_the_recorded_case_name_for_vendor_splits_still_counts_as_known():
    f = base_features()
    del f["vendor_split_rows"]
    f["yahoo_split_event_in_store_pm5d"] = []
    assert {c["check"]: c["result"] for c in validate.negative_check(f, [], _engine("unknown"))}[
        "vendor_split_present"] == "none_found"


def test_genuine_is_refused_when_there_is_no_pack(w):
    res = w.validate(policy_genuine(w), pk=None, engine=engine_b())
    assert not res.ok and has(res, "V7", "negative_check_unknown")
    assert "vendor_split_present" in " ".join(res.errors)


def test_genuine_is_refused_when_the_pack_lacks_the_placeholder_feature(w):
    f = base_features(gross_factor=0.6415)
    del f["placeholder"]
    res = w.validate(policy_genuine(w), pk=pack(f), engine=engine_b())
    assert not res.ok and has(res, "V7", "negative_check_unknown") and "placeholder_present" in " ".join(res.errors)


def test_genuine_is_refused_when_the_pack_lacks_the_vendor_split_feature(w):
    f = base_features(gross_factor=0.6415)
    del f["vendor_split_rows"]
    res = w.validate(policy_genuine(w), pk=pack(f), engine=engine_b())
    assert not res.ok and has(res, "V7", "negative_check_unknown") and "vendor_split_present" in " ".join(res.errors)


def test_genuine_is_refused_when_the_pack_lacks_the_series_feature(w):
    f = base_features(gross_factor=0.6415)
    del f["series"]
    res = w.validate(policy_genuine(w), pk=pack(f), engine=engine_b())
    assert not res.ok and has(res, "V7", "negative_check_unknown") and "series_not_eq" in " ".join(res.errors)


def test_genuine_is_refused_when_there_is_no_engine_proposal(w):
    res = w.validate(policy_genuine(w), pk=pack(base_features(gross_factor=0.6415)), engine=None)
    assert not res.ok and has(res, "V7", "negative_check_unknown") and "engine_rule_r1_r4" in " ".join(res.errors)


def test_genuine_is_refused_when_the_series_is_not_eq(w):
    res = w.validate(policy_genuine(w), pk=pack(base_features(gross_factor=0.6415, series="BE")),
                     engine=engine_b())
    assert not res.ok and has(res, "V7", "negative_check_failed")


def test_a_found_check_is_reported_as_failed_even_when_another_is_unknown(w):
    f = base_features(gross_factor=0.6415, placeholder=True)
    del f["series"]
    res = w.validate(policy_genuine(w), pk=pack(f), engine=engine_b())
    assert has(res, "V7", "negative_check_failed") and has(res, "V7", "negative_check_unknown")


def test_unknown_checks_do_not_block_other_labels(w):
    res = w.validate(spos_demerger(w), pk=None, engine=None)
    assert res.ok, res.errors


# ------------------------------------------------------------------ rule 7: the R7 document window [ex-5d, ex]

def _genuine_with(w, name, peers=3):
    p = policy_genuine(w)
    p["citations"] = [cit("c1", w.ids[name], Q_IRDAI)]
    return w.validate(p, pk=pack(base_features(gross_factor=0.6415, n_peers_moved=peers)), engine=engine_b())


@pytest.mark.parametrize("name", ["irdai_b", "irdai_edge_lo", "irdai_edge_hi"])
def test_r7_a_document_inside_the_window_grades_medium_with_corroboration(w, name):
    res = _genuine_with(w, name)
    assert res.ok, res.errors
    assert res.proposal["classification"]["confidence"] == "medium"
    assert res.proposal["classification"]["rule_id"] == "R7"


@pytest.mark.parametrize("name", ["irdai_old", "irdai_below", "irdai_after"])
def test_r7_a_document_outside_the_window_grades_low_even_with_corroboration(w, name):
    res = _genuine_with(w, name)
    assert res.ok, res.errors          # still a valid, stored claim; it just earns no confidence
    assert res.proposal["classification"]["confidence"] == "low"
    assert res.proposal["classification"]["rule_id"] is None


def test_r7_a_dated_document_outside_the_window_does_not_dilute_one_inside(w):
    p = policy_genuine(w)
    p["citations"] = [cit("c1", w.ids["irdai_old"], Q_IRDAI), cit("c2", w.ids["irdai_b"], Q_IRDAI)]
    p["claims"][0]["citations"] = ["c1", "c2"]
    res = w.validate(p, pk=pack(base_features(gross_factor=0.6415, n_peers_moved=3)), engine=engine_b())
    assert res.ok, res.errors
    assert res.proposal["classification"]["confidence"] == "medium"
    assert res.proposal["classification"]["rule_id"] == "R7"


def test_r7_a_context_only_document_cannot_fire_the_rule(w):
    reg = w.register(title="Background note on insurance distribution", source_class="regulator", subject=SUBJ_B,
                     provenance="human_attach", published_at="2026-10-21", published_at_basis="human_declared",
                     relevance="context", relevance_basis="attacher_declared", entity_match=False,
                     excerpt=EXCERPT_IRDAI)
    w.ids = dict(w.ids, ctx_b=reg.capture_id)
    res = _genuine_with(w, "ctx_b")
    assert res.ok, res.errors
    assert res.proposal["classification"]["confidence"] == "low" and res.proposal["classification"]["rule_id"] is None


def test_r7_the_window_is_judged_by_the_published_day_in_ist(w):
    # 2026-10-17T19:00Z is 2026-10-18 00:30 IST, the first day of the window
    edge = w.register(title="Paper stamped late on the 17th UTC", source_class="regulator", subject=SUBJ_B,
                      provenance="human_attach", published_at="2026-10-17T19:00:00Z",
                      published_at_basis="source", relevance="sector", relevance_basis="attacher_declared",
                      entity_match=False, excerpt=EXCERPT_IRDAI)
    w.ids = dict(w.ids, edge_ist=edge.capture_id)
    assert _genuine_with(w, "edge_ist").proposal["classification"]["confidence"] == "medium"


# ------------------------------------------------------------------ fix round 2: provenance, body, token boundary, integrity

def _spos_fields(**over):
    base = dict(provenance="human_attach", subject=SUBJ_A, url="https://example.test/x", title="NSE circular",
                published_at="2026-10-10T04:00:00Z", published_at_basis="source", source_class="exchange",
                relevance="entity", relevance_basis="token_match", excerpt=EXCERPT_SPOS, entity_match=True,
                typed_fields={"prev_close": 660.75, "spos_price": 400.0, "action_type": "demerger"})
    base.update(over)
    return base


def test_an_agent_attached_exchange_document_cannot_carry_a_value_transfer_proposal(w):
    reg = w.register(**_spos_fields(provenance="agent_attach", published_at_basis="agent_declared"))
    w.ids = dict(w.ids, agent_doc=reg.capture_id)
    res = w.validate(spos_demerger(w, doc="agent_doc"))
    # R-6: the declared class is not verified, so it cannot carry a value-transfer label at all
    assert not res.ok and has(res, "V7", "primary_source_required")


def test_an_agent_declared_date_alone_also_cannot_carry_a_value_transfer_proposal(w):
    reg = w.register(**_spos_fields(published_at_basis="agent_declared"))
    w.ids = dict(w.ids, agent_doc=reg.capture_id)
    res = w.validate(spos_demerger(w, doc="agent_doc"))
    assert not res.ok and has(res, "V7", "primary_source_required")


def test_high_needs_the_private_body_to_still_exist_and_hash(w):
    body = evidence.store_body(w.archive, b"Body that will be deleted: previous close Rs 660.75, SPOS Rs 400.00.")
    reg = w.register(**_spos_fields(body_sha256=body))
    w.ids = dict(w.ids, doc_body=reg.capture_id)
    assert w.validate(spos_demerger(w, doc="doc_body")).proposal["classification"]["confidence"] == "high"
    # no archive directory given: the body cannot be checked, so the grade drops and the note says why
    res = w.validate(spos_demerger(w, doc="doc_body"), archive=False)
    assert res.ok and res.proposal["classification"]["confidence"] == "medium"
    assert any("body_unavailable" in n and reg.capture_id in n for n in res.notes)
    # the file is deleted
    (Path(w.archive) / "triage_private" / body).unlink()
    res = w.validate(spos_demerger(w, doc="doc_body"))
    assert res.ok and res.proposal["classification"]["confidence"] == "medium"
    assert res.proposal["classification"]["rule_id"] == "R2"
    assert any("body_unavailable" in n and reg.capture_id in n for n in res.notes)


def test_a_tampered_body_is_refused_and_never_graded_high(w):
    body = evidence.store_body(w.archive, b"Another body that will be edited: previous close Rs 660.75.")
    reg = w.register(**_spos_fields(body_sha256=body, title="NSE circular with a body that changes"))
    w.ids = dict(w.ids, doc_body2=reg.capture_id)
    (Path(w.archive) / "triage_private" / body).write_bytes(b"something else entirely")
    res = w.validate(spos_demerger(w, doc="doc_body2"))
    assert not res.ok and has(res, "V4", "body_tampered")


def test_quote_in_respects_token_boundaries():
    q = evidence.quote_in
    src = evidence.norm_text("Previous close Rs 1660.75. SPOS price Rs 1400.00 for the demerger.")
    assert q(evidence.norm_text("Previous close Rs 1660.75"), src)
    assert q("rs 1400.00 for the demerger", src)
    assert not q("660.75", src)            # the tail of 1660.75
    assert not q("400.00", src)            # the tail of 1400.00
    assert not q("1660", src)              # the head of 1660.75
    assert not q("75", src)                # the decimals
    assert not q("merger", src)            # the tail of a word
    assert q("1660.75.", src) and q("spos", src)
    assert q("1,660.75", evidence.norm_text("Rs 1,660.75 per share"))
    assert not q("660.75", evidence.norm_text("Rs 1,660.75 per share"))
    assert not q("5", evidence.norm_text("15 shares"))
    assert q("15", evidence.norm_text("15 shares")) and q("15 shares", evidence.norm_text("15 shares"))
    assert q("a-b", evidence.norm_text("see a-b here"))
    assert not q("", src)


def test_rule4_a_quote_that_starts_inside_a_number_is_refused(w):
    reg = w.register(**_spos_fields(excerpt="Previous close Rs 1660.75. SPOS price Rs 1400.00 for the demerger.",
                                    title="NSE circular with longer prices",
                                    typed_fields={"prev_close": 1660.75, "spos_price": 1400.0}))
    w.ids = dict(w.ids, long_prices=reg.capture_id)
    p = spos_demerger(w, doc="long_prices")
    p["citations"][0]["quote"] = "Previous close Rs 1660.75. SPOS price Rs 1400.00"
    p["resolution"]["factor_inputs"] = [{"name": "prev_close", "value": 1660.75, "citation": "c1"},
                                        {"name": "spos_price", "value": 1400.0, "citation": "c1"}]
    p["claims"] = [claim("k1", "The circular gives 1660.75 and 1400.00.", ["c1"])]
    assert w.validate(p).ok
    for bad in ("660.75. SPOS price Rs 1400.00", "Previous close Rs 1660.75. SPOS price Rs 400.00"):
        q = copy.deepcopy(p)
        q["citations"][0]["quote"] = bad
        res = w.validate(q)
        assert not res.ok and has(res, "V4", "c1"), bad


def _corrupt_subject_b_doc(w, how):
    reg = w.register(subject=SUBJ_B, provenance="human_attach", title=f"Undiscovered paper ({how})",
                     source_class="regulator", relevance="sector", relevance_basis="attacher_declared",
                     entity_match=False, published_at="2026-10-20", published_at_basis="human_declared",
                     excerpt="Board approves the demerger of the insurance distribution arm.")
    path = Path(reg.archive_path)
    if how == "delete":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes().replace(b"demerger", b"mergerXX"))
    return reg


@pytest.mark.parametrize("how", ["delete", "tamper"])
def test_a_broken_archived_document_makes_the_demerger_scan_unknown_not_clean(w, how):
    reg = _corrupt_subject_b_doc(w, how)
    res = w.validate(policy_genuine(w), pk=pack(base_features(gross_factor=0.6415)), engine=engine_b())
    assert not res.ok and has(res, "V7", "negative_check_unknown")
    assert "demerger_keyword_in_docs" in " ".join(res.errors)
    assert any("evidence_unreadable" in n and reg.capture_id in n for n in res.notes)


def test_negative_check_marks_the_keyword_scan_unknown_for_integrity_rejects_only():
    f = base_features()
    eng = _engine("unknown", key=KEY_B)

    def scan(rejected):
        return {c["check"]: c for c in validate.negative_check(f, [], eng, rejected)}["demerger_keyword_in_docs"]
    assert scan([])["result"] == "none_found"
    assert scan(None)["result"] == "none_found"
    for reason in ("envelope_file_missing", "envelope_hash_mismatch", "envelope_invalid",
                   "captured_at_unparseable", "published_at_unparseable"):
        got = scan([("TD-0123456789abcdef", reason)])
        assert got["result"] == "unknown" and got["sources"] == ["TD-0123456789abcdef"], reason
    # legitimately later or later-dated documents are not evidence of anything the cohort could see
    assert scan([("TD-1", "captured_after_cutoff"), ("TD-2", "published_after_cutoff"),
                 ("TD-3", "published_after_capture")])["result"] == "none_found"
    # a found keyword still wins over an integrity reject
    hit = {"title": "Demerger record date", "headline": None, "excerpt": None}
    got = {c["check"]: c for c in validate.negative_check(f, [("TD-9", hit)], eng,
                                                          [("TD-1", "envelope_file_missing")])}
    assert got["demerger_keyword_in_docs"]["result"] == "found"


# ------------------------------------------------------------------ R-4, R-5, R-6 (orchestrator rulings)

_VALUE_LABELS = [("demerger", "demerger"), ("scheme", "scheme"), ("rights", "rights"),
                 ("split_or_bonus_missed_by_vendor", "split")]


def _as_label(p, label, kind):
    p["classification"]["label"] = label
    p["resolution"]["resolve_kind"] = kind
    return p


@pytest.mark.parametrize("label,kind", _VALUE_LABELS)
@pytest.mark.parametrize("source_class", ["exchange", "regulator", "company"])
def test_r6_an_agent_declared_primary_link_cannot_carry_a_value_transfer_proposal(w, label, kind, source_class):
    reg = w.register(**_spos_fields(provenance="agent_attach", published_at_basis="agent_declared",
                                    source_class=source_class))
    w.ids = dict(w.ids, agent_doc=reg.capture_id)
    res = w.validate(_as_label(spos_demerger(w, doc="agent_doc"), label, kind))
    assert not res.ok and has(res, "V7", "primary_source_required")


def test_r6_the_same_document_attached_by_a_human_carries_the_proposal(w):
    reg = w.register(**_spos_fields())
    w.ids = dict(w.ids, human_doc=reg.capture_id)
    assert w.validate(spos_demerger(w, doc="human_doc")).ok


def test_r6_a_verified_primary_alongside_an_agent_declared_one_still_carries_it(w):
    reg = w.register(**_spos_fields(provenance="agent_attach", published_at_basis="agent_declared"))
    p = spos_demerger(w)
    p["citations"].append(cit("c2", reg.capture_id, "SPOS price Rs 400.00"))
    p["claims"].append(claim("k3", "The link repeats the SPOS price of 400.00.", ["c2"]))
    assert w.validate(p).ok


def _extra_primary(w, relevance, entity_match, source_class="regulator"):
    reg = w.register(**_spos_fields(source_class=source_class, relevance=relevance, entity_match=entity_match,
                                    relevance_basis="attacher_declared", published_at_basis="human_declared",
                                    title="Regulator paper", typed_fields=None, excerpt=EXCERPT_IRDAI))
    return reg.capture_id


def _with_extra_primary(w, capture_id, label="demerger", kind="demerger"):
    p = _as_label(spos_demerger(w), label, kind)
    p["citations"].append(cit("c2", capture_id, Q_IRDAI))
    p["claims"].append(claim("k3", "The regulator invited comments until 25 October 2026.", ["c2"]))
    return p


@pytest.mark.parametrize("relevance", ["sector", "context"])
@pytest.mark.parametrize("entity_match", [False, None])
def test_r4_every_cited_primary_document_needs_a_verified_entity_match_for_a_demerger(w, relevance, entity_match):
    p = _with_extra_primary(w, _extra_primary(w, relevance, entity_match))
    res = w.validate(p)
    assert not res.ok and has(res, "V8", "entity_match") and has(res, "V8", "c2")
    assert not has(res, "V8", "c1")


@pytest.mark.parametrize("label,kind", _VALUE_LABELS)
def test_r4_applies_to_every_value_transfer_label(w, label, kind):
    p = _with_extra_primary(w, _extra_primary(w, "sector", False), label, kind)
    res = w.validate(p)
    assert not res.ok and has(res, "V8", "c2")


@pytest.mark.parametrize("source_class", ["exchange", "regulator", "company"])
def test_r4_holds_for_every_primary_class(w, source_class):
    p = _with_extra_primary(w, _extra_primary(w, "context", False, source_class))
    assert has(w.validate(p), "V8", "c2")


def test_r4_an_entity_relevance_failure_is_reported_once_not_twice(w):
    p = _with_extra_primary(w, _extra_primary(w, "entity", False))
    assert sum(1 for e in w.validate(p).errors if e.startswith("V8:") and "c2" in e) == 1


def test_r4_a_primary_document_with_a_verified_match_is_fine_whatever_its_relevance(w):
    p = _with_extra_primary(w, _extra_primary(w, "sector", True))
    assert w.validate(p).ok


def test_r4_a_non_primary_document_without_a_match_is_not_held_to_it(w):
    reg = w.register(**_spos_fields(source_class="news", relevance="context", entity_match=False,
                                    relevance_basis="attacher_declared", title="Market wrap", typed_fields=None,
                                    excerpt=EXCERPT_IRDAI))
    assert w.validate(_with_extra_primary(w, reg.capture_id)).ok


def test_r4_does_not_apply_to_labels_that_are_not_value_transfer(w):
    # a sector regulator paper with entity_match False backs genuine_event (see the policy happy path)
    assert w.validate(policy_genuine(w), pk=pack(base_features(gross_factor=0.6415)), engine=engine_b()).ok


def _llm_ctx(w, name="test-model"):
    return RunContext(as_of=AS_OF, kind="triage", track="live", cfg=w.cfg, clock=FrozenClock(NOON),
                      actor=Actor(kind="llm", name=name))


def _stored_rows(w):
    conn = connect(w.cfg.paths.db, readonly=True)
    try:
        return conn.execute("SELECT count(*) FROM proposals").fetchone()[0]
    finally:
        conn.close()


def test_r5_write_proposal_refuses_an_invalid_llm_proposal_called_directly(w):
    p = spos_demerger(w)
    p["citations"][0]["quote"] = "Previous close Rs 999.99. SPOS price Rs 1.00"       # not in the archived text
    with _llm_ctx(w) as ctx:
        with pytest.raises(Refused) as ei:
            store.write_proposal(ctx, p, [])
    assert ei.value.code == "triage_proposal_invalid" and "V4" in ei.value.detail
    assert _stored_rows(w) == 0


@pytest.mark.parametrize("breaker,rule", [
    (lambda w, p: p["citations"].__setitem__(0, cit("c1", "TD-" + "0" * 16, Q_SPOS)), "V2"),
    (lambda w, p: p["citations"].__setitem__(0, cit("c1", w.ids["late_a"], Q_SPOS)), "V2"),
    (lambda w, p: p.__setitem__("as_of", "2026-10-29"), "V1"),
    (lambda w, p: p["resolution"].__setitem__("factor", 9.0), "V6"),
    (lambda w, p: p["claims"][0].__setitem__("text", "The circular gives a previous close of 12345."), "V5"),
])
def test_r5_every_validator_rule_is_enforced_at_the_store(w, breaker, rule):
    p = spos_demerger(w)
    breaker(w, p)
    with _llm_ctx(w) as ctx:
        with pytest.raises(Refused) as ei:
            store.write_proposal(ctx, p, [])
    assert ei.value.code == "triage_proposal_invalid" and rule + ":" in ei.value.detail
    assert _stored_rows(w) == 0


def test_r5_a_caller_cannot_skip_validation_by_passing_validated_looking_fields(w):
    p = spos_demerger(w)
    p["resolution"]["factor"] = 660.75 / 400.0
    p["citations"][0]["quote"] = "Previous close Rs 660.75. SPOS price Rs 401.00"
    with _llm_ctx(w) as ctx:
        with pytest.raises(Refused):
            store.write_proposal(ctx, p, [{"capture_id": w.ids["spos_body"], "sha256": "0" * 64}], flags=["OK"])
    assert _stored_rows(w) == 0


def test_r5_the_store_keeps_the_engine_derived_fields_not_the_callers(w):
    p = spos_demerger(w, doc="spos_url")                      # link only: the engine grades it medium
    p["producer"]["self_confidence"] = "high"
    p["classification"].update(confidence="high", rule_id=None)
    with _llm_ctx(w) as ctx:
        stored = store.write_proposal(ctx, p, [])
        row = store.get_proposal(ctx.conn, stored.proposal_id)
        sha = evidence.get_capture(ctx.conn, w.ids["spos_url"])["sha256"]
    assert row["payload"]["classification"]["confidence"] == "medium"
    assert row["payload"]["classification"]["rule_id"] == "R2"
    assert row["payload"]["resolution"]["factor"] == pytest.approx(660.75 / 400.0, abs=1e-6)
    assert row["evidence"] == [{"capture_id": w.ids["spos_url"], "sha256": sha}]


def test_r5_the_store_validates_against_the_pack_it_is_given(w):
    p = spos_demerger(w, doc="company_demerger")
    p["resolution"].update(factor_basis="market_implied", factor_inputs=[], factor=None)
    p["citations"] = [cit("c1", w.ids["company_demerger"], "The board approved the demerger")]
    p["claims"] = [claim("k1", "The board approved the demerger.", ["c1"])]
    with _llm_ctx(w) as ctx:
        with pytest.raises(Refused) as ei:
            store.write_proposal(ctx, p, [])                  # no pack: no gross_factor, so no market-implied factor
    assert "V6" in ei.value.detail
    with _llm_ctx(w) as ctx:
        stored = store.write_proposal(ctx, p, [], pack=pack())
        row = store.get_proposal(ctx.conn, stored.proposal_id)
    assert row["payload"]["resolution"]["factor"] == pytest.approx(1 / 0.5985, abs=1e-5)


def test_r5_the_store_loads_the_engine_proposal_and_pack_itself(w):
    """No pack or engine proposal is passed: the store reads them as 'triage submit' does."""
    from quant.triage import pack as triage_pack

    feats = base_features(gross_factor=0.6415, n_peers_moved=3)
    pack_doc = pack(feats)
    sha, raw = store.pack_bytes(pack_doc)
    eng = _engine("unknown", key=KEY_B)
    eng["producer"]["pack_sha256"] = sha
    with RunContext(as_of=AS_OF, kind="triage", track="live", cfg=w.cfg, clock=FrozenClock(NOON),
                    actor=Actor(kind="system", name="engine")) as ctx:
        eng_stored = store.write_proposal(ctx, eng, [])
    path = triage_pack.pack_path(w.cfg.paths.data_dir, eng_stored.proposal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    p = policy_genuine(w)
    p["producer"]["pack_sha256"] = sha
    with _llm_ctx(w) as ctx:
        stored = store.write_proposal(ctx, p, [])
        got = store.get_proposal(ctx.conn, stored.proposal_id)
    assert got["payload"]["classification"]["confidence"] == "medium"        # corroboration came from the pack
    # a pack that no longer hashes to the engine's pack_sha256 is not trusted: genuine_event cannot pass
    path.write_bytes(raw + b" ")
    with _llm_ctx(w) as ctx:
        with pytest.raises(Refused) as ei:
            store.write_proposal(ctx, policy_genuine(w, producer=dict(p["producer"], name="llm:other")), [])
    assert ei.value.code == "triage_proposal_invalid"


def test_r5_engine_proposals_are_not_validated_by_the_agent_validator(w):
    with RunContext(as_of=AS_OF, kind="triage", track="live", cfg=w.cfg, clock=FrozenClock(NOON),
                    actor=Actor(kind="system", name="engine")) as ctx:
        assert store.write_proposal(ctx, _engine("unknown"), []).created is True


# ------------------------------------------------------------------ adversarial review fixes (GOV-1, F2, F3, ADV-PIT-1)

GOOD_PACK_B = lambda **over: pack(base_features(gross_factor=0.6415, n_peers_moved=3, **over))  # noqa: E731


def test_the_baseline_genuine_proposal_for_the_review_fixes_passes(w):
    assert w.validate(policy_genuine(w), pk=GOOD_PACK_B(), engine=engine_b()).ok


@pytest.mark.parametrize("over, check", [
    ({"cross_section_count": 5, "market_median_move_pct": 0.2}, "cross_section_jumps_present"),
    ({"reversal_prior": True}, "reversal_pair_present"),
    ({"reversal_forward": True}, "reversal_pair_present"),
])
def test_genuine_is_refused_when_the_engine_sees_a_vendor_artifact_in_the_prices(w, over, check):
    """GOV-1: an attached document must not hide a cross-section or reversal finding from the validator."""
    res = w.validate(policy_genuine(w), pk=GOOD_PACK_B(**over), engine=engine_b())
    assert not res.ok and has(res, "V7", "negative_check_failed") and check in " ".join(res.errors)


@pytest.mark.parametrize("over", [{"cross_section_count": 2}, {"cross_section_count": 5, "market_median_move_pct": -2.5}])
def test_a_small_cross_section_or_a_moving_market_does_not_block_genuine(w, over):
    assert w.validate(policy_genuine(w), pk=GOOD_PACK_B(**over), engine=engine_b()).ok


def test_genuine_with_an_unknown_cross_section_or_reversal_is_refused_as_unknown(w):
    f = base_features(gross_factor=0.6415, n_peers_moved=3)
    del f["cross_section_count"]
    res = w.validate(policy_genuine(w), pk=pack(f), engine=engine_b())
    assert has(res, "V7", "negative_check_unknown") and "cross_section_jumps_present" in " ".join(res.errors)
    f = base_features(gross_factor=0.6415, n_peers_moved=3)
    del f["reversal_prior"]
    res = w.validate(policy_genuine(w), pk=pack(f), engine=engine_b())
    assert has(res, "V7", "negative_check_unknown") and "reversal_pair_present" in " ".join(res.errors)


def _register_b(w, **over):
    base = dict(provenance="human_attach", subject=SUBJ_B, url="https://example.test/b", title="Company notice",
                published_at="2026-10-20", published_at_basis="human_declared", source_class="company",
                relevance="entity", relevance_basis="token_match", excerpt=None, entity_match=True)
    base.update(over)
    return w.register(**base).capture_id


@pytest.mark.parametrize("title", ["TESTCO announces bonus issue 1:1", "TESTCO board approves stock split 1:2",
                                   "TESTCO rights issue of equity shares"])
def test_genuine_is_refused_when_any_admissible_document_announces_a_split_bonus_or_rights(w, title):
    """F2: the keyword check is engine-run over every admissible document, cited or not."""
    cid = _register_b(w, title=title)
    res = w.validate(policy_genuine(w), pk=GOOD_PACK_B(), engine=engine_b())
    assert not res.ok and has(res, "V7", "negative_check_failed") and "demerger_keyword_in_docs" in " ".join(res.errors)
    keyword = {c["check"]: c for c in validate.negative_check(
        base_features(), [(cid, {"title": title, "typed_fields": {}})], _engine("unknown"))}["demerger_keyword_in_docs"]
    assert keyword["result"] == "found" and keyword["sources"] == [cid]


def test_genuine_is_refused_when_a_document_carries_a_typed_action_type(w):
    _register_b(w, typed_fields={"action_type": "bonus"})
    res = w.validate(policy_genuine(w), pk=GOOD_PACK_B(), engine=engine_b())
    assert has(res, "V7", "negative_check_failed")


def _body_envelope(w, text, **over):
    sha = evidence.store_body(w.archive, text.encode("utf-8"))
    cid = _register_b(w, title="Company: stock exchange intimation dated 20 Oct 2026", body_sha256=sha, **over)
    return cid, sha


def test_genuine_is_refused_when_the_private_body_announces_a_demerger(w):
    """F3: a neutral title and no excerpt, but the archived body announces the demerger."""
    _body_envelope(w, "The Board approved a Scheme of Arrangement for the demerger; record date to be fixed.")
    res = w.validate(policy_genuine(w), pk=GOOD_PACK_B(), engine=engine_b())
    assert not res.ok and has(res, "V7", "negative_check_failed") and "demerger_keyword_in_docs" in " ".join(res.errors)


def test_a_clean_private_body_does_not_block_genuine(w):
    _body_envelope(w, "Quarterly results and a note on the distribution industry.")
    assert w.validate(policy_genuine(w), pk=GOOD_PACK_B(), engine=engine_b()).ok


def test_genuine_is_unknown_when_a_named_body_cannot_be_read(w):
    _body_envelope(w, "Quarterly results and a note on the distribution industry.")
    res = w.validate(policy_genuine(w), pk=GOOD_PACK_B(), engine=engine_b(), archive=False)
    assert not res.ok and has(res, "V7", "negative_check_unknown") and "demerger_keyword_in_docs" in " ".join(res.errors)


def test_negative_check_marks_the_keyword_scan_unknown_for_an_unreadable_body(tmp_path):
    f = base_features()
    env = {"title": "Notice", "typed_fields": {}, "body_sha256": "a" * 64}
    got = {c["check"]: c for c in validate.negative_check(f, [("TD-" + "1" * 16, env)], _engine("unknown"),
                                                          archive_dir=tmp_path)}["demerger_keyword_in_docs"]
    assert got["result"] == "unknown" and got["sources"] == ["TD-" + "1" * 16]


@pytest.mark.parametrize("breaker", [
    lambda pk, eng: pk.update(as_of="2026-09-30"),
    lambda pk, eng: pk.pop("as_of"),
    lambda pk, eng: pk.update(cutoff_utc="2026-09-30T18:29:59.999999Z"),
    lambda pk, eng: pk.pop("cutoff_utc"),
    lambda pk, eng: eng.update(as_of="2026-09-30"),
], ids=["pack_as_of", "pack_no_as_of", "pack_cutoff", "pack_no_cutoff", "engine_as_of"])
def test_adv_pit_1_a_proposal_is_validated_only_against_this_cohorts_pack_and_engine_row(w, breaker):
    """ADV-PIT-1: a pack or engine row built for another cohort carries stale features; refuse and say 'build first'."""
    pk, eng = GOOD_PACK_B(), engine_b()
    breaker(pk, eng)
    res = w.validate(policy_genuine(w), pk=pk, engine=eng)
    assert not res.ok and has(res, "V1", "binding") and "build" in " ".join(res.errors)


def test_adv_pit_1_the_matching_cohort_is_accepted(w):
    assert w.validate(policy_genuine(w), pk=GOOD_PACK_B(), engine=engine_b()).ok
