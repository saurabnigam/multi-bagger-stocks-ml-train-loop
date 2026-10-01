"""Evidence triage WP6: the eval harness (scripts/triage_eval.py), the golden set and the agent contract.

Everything here is offline. The golden set is 36 recorded anomalies split into ``inputs.json`` (price
features only) and ``labels.json`` (never read by the classifier). T0 replays the price-only rules, T1
adds hand-written document fixtures (synthetic text, not point-in-time true), ``score-llm`` replays
recorded agent runs through the real validator against a disposable state database, and
``live-report`` reads a read-only copy. No test touches the network, a real database or a model.
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import importlib.util
import json
import re
import shutil
import socket
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from quant.config import load as load_config
from quant.db.core import apply_schema, connect, update_control
from quant.run import RunContext
from quant.triage import store as triage_store, types as T
from quant.types import Actor, FrozenClock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "triage_eval.py"
DOCS = ROOT / "docs" / "operations"
CONTRACT = DOCS / "triage_agent_contract.md"
PROMPT = DOCS / "triage_agent_prompt.md"
SCHEMA_FILE = DOCS / "triage_proposal.schema.json"
WRAPPER = ROOT / ".claude" / "agents" / "evidence-triage.md"


def _load_script():
    spec = importlib.util.spec_from_file_location("triage_eval", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["triage_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


te = _load_script()

@pytest.fixture(autouse=True)
def _fast_run_context(monkeypatch):
    """Every RunContext hashes the whole package and shells out to git; neither matters here (amendment F7)."""
    import quant.db.core as dbcore
    monkeypatch.setattr(dbcore, "code_sha256", lambda root=None: "0" * 64)
    monkeypatch.setattr(RunContext, "_get_git_sha", staticmethod(lambda: "test"))


GROUND_TRUTH = ("TMPV_2025-10-14", "VEDL_2026-04-30", "TRENT_2026-01-01", "HEG_2026-09-07", "POLICYBZR_2026-09-24")
T1_CASES = ("TMPV_2025-10-14", "VEDL_2026-04-30", "ABFRL_2025-05-22", "HEG_2026-09-07", "TRENT_2026-01-01",
            "POLICYBZR_2026-09-24", "IEX_2025-07-24")


@pytest.fixture(scope="module")
def golden():
    return te.load_golden()


def _run(argv, capsys):
    code = te.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def _json_run(capsys, *argv):
    code, out, err = _run([*argv, "--json"], capsys)
    return code, (json.loads(out) if out.strip() else None), err


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a test reached for the network")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ------------------------------------------------------------------ statistics

def test_wilson_interval_matches_hand_values_and_keeps_unknown_unknown():
    lo, hi = te.wilson(5, 10)                                                 # textbook value, not an eval result
    assert (round(lo, 4), round(hi, 4)) == (0.2366, 0.7634)
    lo, hi = te.wilson(10, 10)
    assert round(lo, 2) == 0.72 and hi == pytest.approx(1.0)                  # all successes still leave a lower bound
    lo, hi = te.wilson(0, 10)
    assert lo == pytest.approx(0.0) and hi < 0.31
    assert te.wilson(0, 0) is None                                            # no sample is unknown, not [0, 0]
    with pytest.raises(ValueError):
        te.wilson(5, 3)


def test_a_rate_with_no_denominator_is_none_not_zero():
    assert te.rate_block(0, 0) == {"k": 0, "n": 0, "rate": None, "wilson95": None}
    b = te.rate_block(3, 4)
    assert b["rate"] == 0.75 and len(b["wilson95"]) == 2


def _dec(label, conf, action=None, factor=None):
    return {"label": label, "confidence": conf, "action": action, "factor": factor}


@pytest.mark.parametrize("pred, conf, truth, expected", [
    ("demerger", "medium", "genuine_event", "value_transfer_on_non_transfer"),
    ("scheme", "high", "pending_listing", "value_transfer_on_non_transfer"),
    ("rights", "medium", "vendor_error_other", "value_transfer_on_non_transfer"),
    ("demerger", "low", "genuine_event", None),                               # low prints no resolve command
    ("demerger", "high", "scheme", None),                                     # both value transfer: a wrong kind, not this danger
    ("genuine_event", "low", "demerger", "genuine_on_value_transfer"),        # any confidence
    ("genuine_event", "high", "scheme", "genuine_on_value_transfer"),
    ("genuine_event", "medium", "vendor_error_other", None),
    ("unknown", "low", "demerger", None),                                     # abstaining is never dangerous
    ("vendor_error_other", "medium", "demerger", None),
])
def test_dangerous_error_definition(pred, conf, truth, expected):
    assert te.is_dangerous(_dec(pred, conf), truth) == expected


@pytest.mark.parametrize("decision, truth, expected", [
    # a factor applied under a label that is not a value-transfer label still changes a security's TRI
    (_dec("vendor_error_other", "medium", "resolve", 0.9), "genuine_event", "spurious_adjustment"),
    (_dec("vendor_error_other", "high", "resolve", 1.6519), "pending_listing", "spurious_adjustment"),
    (_dec("split_or_bonus_missed_by_vendor", "medium", "resolve", 0.5), "vendor_error_other", "spurious_adjustment"),
    (_dec("surveillance_or_series_change", "high", "resolve", 2.0), "genuine_event", "spurious_adjustment"),
    (_dec("vendor_error_other", "low", "resolve", 0.9), "genuine_event", None),          # low prints no resolve command
    (_dec("vendor_error_other", "medium", "wait", 0.9), "genuine_event", None),          # not a resolve
    (_dec("vendor_error_other", "medium", "resolve", None), "genuine_event", None),      # resolve with no factor changes nothing
    (_dec("genuine_event", "medium", "resolve", 1.0), "genuine_event", None),            # the genuine resolution: factor 1.0
    (_dec("vendor_error_other", "medium", "resolve", 0.9), "demerger", None),            # the truth is a transfer: a factor is not spurious
    (_dec("demerger", "medium", "resolve", 0.9), "genuine_event", "value_transfer_on_non_transfer"),   # label rule wins
])
def test_a_factor_applied_under_any_label_is_a_spurious_adjustment(decision, truth, expected):
    assert te.is_dangerous(decision, truth) == expected


# ------------------------------------------------------------------ golden set and manifest

def test_golden_is_the_36_cases_split_into_inputs_and_labels(golden):
    assert len(golden.inputs) == 36 and list(golden.labels) == [c["case_id"] for c in golden.inputs]
    banned = [k for c in golden.inputs for k in c if k.startswith(("label_", "yahoo_live")) or k in ("notes", "label_class")]
    assert banned == []                                      # nothing that reveals the answer, nothing post-cutoff
    assert {c["origin"] for c in golden.inputs} == {"recorded", "synthetic"}
    assert [c["origin"] for c in golden.inputs if c["origin"] == "synthetic"] == ["synthetic"]
    assert all({"label_class", "label_confidence"} <= set(v) for v in golden.labels.values())
    by = Counter(v["label_class"] for v in golden.labels.values())
    assert by == {"genuine_event": 18, "vendor_error_other": 10, "demerger": 6, "pending_listing": 1, "scheme": 1}
    assert sum(1 for v in golden.labels.values() if v["label_confidence"] == "low") == 4


def test_manifest_pins_file_hashes_versions_and_the_split(golden):
    man = golden.manifest
    assert man["golden_version"] == 1 and man["label_rev"] == 2 and man["n_cases"] == 36      # rev 2: ABFRL label (R-14)
    for name, sha in man["files"].items():
        assert te.sha256_file(te.GOLDEN_DIR / name) == sha
    assert set(man["files"]) == {"inputs.json", "labels.json", "t1_documents.json"}
    assert golden.manifest_sha256 == te.sha256_file(te.GOLDEN_DIR / "MANIFEST.json")


def test_manifest_lists_the_fields_dropped_from_the_copy_and_says_t1_figures_come_from_labels(golden):
    src = golden.manifest["source"]
    assert set(src["fields_dropped"]) == {"security_id", "nse_sector", "sector_group_is_current_not_pit"}
    assert "not point-in-time" in src["fields_dropped_note"]                   # what sector_group_is_current_not_pit records
    assert any("T1" in n and "labels" in n and "circular" in n for n in golden.manifest["notes"])
    assert golden.manifest["holdout_report_sha256"] is None                    # set once, by the single holdout look
    kept = {k for c in golden.inputs for k in c} | {k for v in golden.labels.values() for k in v}
    assert not kept & set(src["fields_dropped"])


# ---- R-14: ABFRL special pre-open price is 98.00 (NSE bhavcopy 22-May-2025: PREV_CLOSE 268.95, OPEN 98.00, HIGH 98.00)

def test_abfrl_label_uses_the_nse_special_pre_open_price_of_98_and_the_factor_2_7444(golden):
    lab = golden.labels["ABFRL_2025-05-22"]
    assert lab["label_factor"] == round(268.95 / 98.00, 4) == 2.7444
    src = lab["label_factor_source"]
    assert "98.00" in src and "268.95" in src and "bhavcopy" in src.lower()
    alts = lab["label_factor_alternatives"]
    assert alts["market_implied_1_over_gross"] == 2.9933
    assert alts["market_implied_vs_exchange_pct"] == pytest.approx(9.07, abs=0.01)     # not +10.18
    assert "+9.1%" in lab["notes"] or "9.1%" in lab["notes"]
    (doc,) = golden.documents["cases"]["ABFRL_2025-05-22"]["documents"]
    assert doc["typed_fields"] == {"prev_close": 268.95, "spos_price": 98.0, "action_type": "demerger"}
    assert "Rs 98.00" in doc["excerpt"] and doc["synthetic"] is True
    assert golden.manifest["label_rev"] == 2                                    # a changed label raises label_rev


def test_the_abfrl_label_change_is_recorded_in_the_manifest_hashes(golden):
    """The manifest pins the new label bytes: case_label_sha256 for ABFRL matches labels.json, inputs are untouched."""
    cid = "ABFRL_2025-05-22"
    assert golden.manifest["case_label_sha256"][cid] == te._sha(golden.labels[cid])
    assert golden.manifest["case_input_sha256"][cid] == te._sha(golden.by_id[cid])     # append-only: inputs never change
    assert golden.by_id[cid]["close_prev"] == 268.95


STALE_ABFRL = re.compile(r"2\.7167|10\.18\b|\+10\.2%|Rs 99(?:\.\d+)?\b|99\.00 for the demerger|\b99\.5\b")


def test_no_fixture_run_or_document_still_pins_the_superseded_abfrl_figures():
    """2.7167 assumed a special pre-open price of 99 (and +10.2% against it); the price is 98.00 and the gap +9.1%."""
    files = [*sorted((ROOT / "tests" / "fixtures" / "triage").rglob("*.json")), *sorted(DOCS.glob("*")),
             ROOT / "docs" / "analysis" / "verification_2026-09-24.md", WRAPPER]
    hits = [f"{f.relative_to(ROOT)}: {m.group(0)}" for f in files if f.is_file()
            for m in STALE_ABFRL.finditer(f.read_text(encoding="utf-8"))]
    assert hits == []


# ---- R-15: exchange-derived factors are the recommendation; the market-implied ones are superseded, with the reason

VERIFICATION = ROOT / "docs" / "analysis" / "verification_2026-09-24.md"


def _recommended_rows():
    text = VERIFICATION.read_text(encoding="utf-8")
    block = text.split("**Recommended corporate-action factors (T2).**", 1)[1].split("Counterfactual", 1)[0]
    return block, {line.split()[0]: line for line in block.splitlines()
                   if line.split() and line.split()[0] in ("TMPV", "VEDL", "TRENT", "HEG")}


def test_the_verification_report_recommends_the_exchange_derived_factors():
    block, rows = _recommended_rows()
    assert re.search(r"TMPV\s+2025-10-14\s+demerger\s+1\.6519\s", rows["TMPV"]) and "660.75" in rows["TMPV"] \
        and "400.00" in rows["TMPV"]
    assert re.search(r"VEDL\s+2026-04-30\s+demerger\s+2\.6722\s", rows["VEDL"]) and "773.60" in rows["VEDL"] \
        and "289.50" in rows["VEDL"]
    assert round(660.75 / 400.00, 4) == 1.6519 and round(773.60 / 289.50, 4) == 2.6722
    assert "1.6708" not in rows["TMPV"] and "2.849" not in rows["VEDL"]          # no longer the recommended value
    assert "TRENT" in rows and "1.5" in rows["TRENT"]                             # the other rows are untouched


def test_the_verification_report_keeps_the_market_implied_factors_as_superseded_with_the_reason():
    block, _ = _recommended_rows()
    flat = " ".join(block.split())                                                # a line break may fall inside a phrase
    assert "1.6708" in flat and "2.849" in flat and "superseded" in flat.lower()
    assert "special pre-open price" in flat and "NSE Indices" in flat
    assert "-1.14%" in flat and "-6.20%" in flat and "real return" in flat        # the move after discovery is return
    assert "secondary sources" in flat and "Yahoo" in flat and "Angel One" in flat and "Upstox" in flat \
        and "Republic" in flat
    assert re.search(r"NSE circular[^.]*(?:was|were) not read", flat)             # the primary text was not opened


def test_a_tampered_golden_file_is_refused(tmp_path):
    shutil.copytree(te.GOLDEN_DIR, tmp_path / "g")
    labels = json.loads((tmp_path / "g" / "labels.json").read_text())
    labels["TMPV_2025-10-14"]["label_class"] = "genuine_event"
    (tmp_path / "g" / "labels.json").write_text(json.dumps(labels))
    with pytest.raises(te.GoldenError, match="labels.json"):
        te.load_golden(tmp_path / "g")


def test_split_is_frozen_by_case_id_hash_with_ground_truth_forced_into_holdout(golden):
    man = golden.manifest["split"]
    dev, hold = set(man["dev"]), set(man["holdout"])
    assert len(dev) == 25 and len(hold) == 11 and not dev & hold and dev | hold == set(golden.labels)
    assert tuple(man["ground_truth_cases"]) == GROUND_TRUTH and set(GROUND_TRUTH) <= hold
    for cid in golden.labels:                                # the committed list is exactly what the rule gives
        assert te.assign_split(cid) == ("holdout" if cid in hold else "dev")
    assert te.split_bucket("ADANIENT_2015-06-03") == te.split_bucket("ADANIENT_2015-06-03")
    # without the forced five the hash alone decides, so the rule is not secretly a hand-picked list
    by_hash = {c for c in golden.labels if te.split_bucket(c) < te.SPLIT_HOLDOUT_BELOW}
    assert hold == by_hash | set(GROUND_TRUTH)


def test_manifest_write_is_idempotent_and_append_only(tmp_path):
    g = tmp_path / "g"
    shutil.copytree(te.GOLDEN_DIR, g)
    before = (g / "MANIFEST.json").read_bytes()
    te.write_manifest(g)
    assert (g / "MANIFEST.json").read_bytes() == before       # nothing changed, nothing rewritten differently

    inputs = json.loads((g / "inputs.json").read_text())
    labels = json.loads((g / "labels.json").read_text())
    new = dict(inputs[0], case_id="ZZNEW_2026-10-01", symbol="ZZNEW")
    inputs.append(new)
    labels["ZZNEW_2026-10-01"] = dict(labels[inputs[0]["case_id"]])
    (g / "inputs.json").write_text(json.dumps(inputs, indent=1) + "\n")
    (g / "labels.json").write_text(json.dumps(labels, indent=1) + "\n")
    te.write_manifest(g)
    man = json.loads((g / "MANIFEST.json").read_text())
    old = json.loads(before)
    assert man["n_cases"] == 37
    assert set(old["split"]["dev"]) <= set(man["split"]["dev"]) and set(old["split"]["holdout"]) <= set(man["split"]["holdout"])
    assert "ZZNEW_2026-10-01" in man["split"]["dev"] + man["split"]["holdout"]
    te.load_golden(g)                                          # the regenerated manifest verifies


def test_manifest_write_refuses_to_drop_a_case_or_edit_its_inputs(tmp_path):
    g = tmp_path / "g"
    shutil.copytree(te.GOLDEN_DIR, g)
    inputs = json.loads((g / "inputs.json").read_text())
    edited = copy.deepcopy(inputs)
    edited[0]["gross_factor"] = 0.5
    (g / "inputs.json").write_text(json.dumps(edited, indent=1) + "\n")
    with pytest.raises(te.GoldenError, match="inputs of an existing case"):
        te.write_manifest(g)
    (g / "inputs.json").write_text(json.dumps(inputs[1:], indent=1) + "\n")
    labels = json.loads((g / "labels.json").read_text())
    labels.pop(inputs[0]["case_id"])
    (g / "labels.json").write_text(json.dumps(labels, indent=1) + "\n")
    with pytest.raises(te.GoldenError, match="removed"):
        te.write_manifest(g)


def test_a_label_change_needs_a_higher_label_rev(tmp_path):
    g = tmp_path / "g"
    shutil.copytree(te.GOLDEN_DIR, g)
    labels = json.loads((g / "labels.json").read_text())
    labels["TMPV_2025-10-14"]["label_confidence"] = "medium"
    (g / "labels.json").write_text(json.dumps(labels, indent=1) + "\n")
    with pytest.raises(te.GoldenError, match="label_rev"):
        te.write_manifest(g)
    te.write_manifest(g, label_rev=3)                          # the committed set is already at rev 2 (R-14)
    assert json.loads((g / "MANIFEST.json").read_text())["label_rev"] == 3


# ------------------------------------------------------------------ T0: price-only rules on the recorded rows

def test_t0_day0_dev_baseline(golden):
    # Aggregate numbers are pinned on the DEV split only (fix round 2, F3). The holdout is looked at once, by
    # 'holdout --once'; a pytest run must not hand anyone tuning thresholds a holdout-inclusive number.
    rep = te.run_report(golden, set_name="dev", tier="t0", day="day0")
    a = rep["all"]
    assert a["n_cases"] == 25
    assert (a["coverage"]["k"], a["coverage"]["n"]) == (9, 25)
    assert (a["precision_fired"]["k"], a["precision_fired"]["n"]) == (9, 9)
    assert a["accuracy_abstain_wrong"]["k"] == 9 and a["accuracy_abstain_wrong"]["n"] == 25
    assert a["dangerous_errors"]["count"] == 0
    assert {r: v["fired"] for r, v in a["by_rule"].items()} == {"R8": 4, "R5": 3, "R6": 2}
    assert a["abstained_by_label"] == {"genuine_event": 9, "demerger": 3, "vendor_error_other": 4}
    assert rep["gates"] == {"dangerous_errors": 0, "passed": True}


def test_t0_day5_dev_adds_only_the_two_correct_reversal_calls(golden):
    d0 = {r["case_id"]: r for r in te.run_report(golden, set_name="dev", tier="t0", day="day0")["cases"]}
    rep = te.run_report(golden, set_name="dev", tier="t0", day="day5")
    fired = {r["case_id"] for r in rep["cases"] if not r["abstained"]}
    assert len(fired) == 11 and rep["all"]["precision_fired"]["rate"] == 1.0
    assert {c for c in fired if d0[c]["abstained"]} == {"INDIAMART_2019-10-27", "INDIAMART_2020-11-14"}
    assert rep["all"]["dangerous_errors"]["count"] == 0


def test_precision_by_confidence_keeps_an_empty_bucket_unknown(golden):
    rep = te.run_report(golden, set_name="dev", tier="t0", day="day0")
    p = rep["all"]["precision_by_confidence"]
    assert p["high"] == {"k": 0, "n": 0, "rate": None, "wilson95": None}      # price-only never reaches high
    assert p["medium"] == {"k": 0, "n": 0, "rate": None, "wilson95": None}
    assert p["low"]["n"] == 9 and p["low"]["k"] == 9


def test_headline_scope_drops_the_low_confidence_labels(golden):
    rep = te.run_report(golden, set_name="dev", tier="t0", day="day0")
    low = sum(1 for r in rep["cases"] if r["label_confidence"] == "low")
    assert rep["all"]["n_cases"] == 25 and rep["headline"]["n_cases"] == 25 - low and low >= 1
    assert rep["headline"]["dangerous_errors"]["count"] == 0
    assert rep["unsampled_classes"] == ["rights", "split_or_bonus_missed_by_vendor", "surveillance_or_series_change"]


def test_unknown_abstention_is_never_counted_as_a_correct_or_wrong_call(golden):
    rep = te.run_report(golden, set_name="dev", tier="t0", day="day0")
    abst = [r for r in rep["cases"] if r["abstained"]]
    assert len(abst) == 16 and all(r["predicted"] == "unknown" and r["correct"] is None for r in abst)
    assert all(r["correct"] is True for r in rep["cases"] if not r["abstained"])


def test_t1_dev_regression_guard_for_the_document_rules(golden, no_network):
    """The T1 half of the regression guard, on the DEV split only (R-11). Holdout numbers are never asserted."""
    t1 = te.run_report(golden, set_name="dev", tier="t1", day="day0")
    (row,) = t1["cases"]
    assert row["case_id"] == "ABFRL_2025-05-22"
    assert (row["predicted"], row["confidence"], row["rule_id"]) == ("demerger", "medium", "R2")   # circular attached as a link
    assert row["price_only_label"] == "unknown"                                # prices alone cannot name a demerger
    assert (t1["all"]["accuracy_abstain_wrong"]["k"], t1["all"]["accuracy_abstain_wrong"]["n"]) == (1, 1)
    assert t1["gates"]["passed"] is True
    f = t1["factor_check"]                                                    # the exchange-derived label, inside 2 percent
    assert f["n"] == 1 and f["max_abs_error_pct"] < 0.01 and f["within_2pct"] == 1 and f["tolerance_pct"] == 2.0
    assert row["factor"] == pytest.approx(268.95 / 98.00) and row["label_factor"] == 2.7444
    d5 = te.run_report(golden, set_name="dev", tier="t0", day="day5")
    assert d5["gates"]["passed"] is True


def _test_files():
    return sorted((ROOT / "tests" / "unit").glob("test_triage_*.py"))


#: tests that may build predictions from every case because they assert equality, never a metric
_MAY_READ_EVERY_CASE = {"test_predictions_depend_on_inputs_only_never_on_labels"}


def _holdout_violations(source: str, filename: str = "<src>") -> list[str]:
    banned = ("holdout", "all")
    bad = []
    tree = ast.parse(source)
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
            name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
            consts = [a.value for a in call.args if isinstance(a, ast.Constant)]
            consts += [k.value.value for k in call.keywords if isinstance(k.value, ast.Constant)]
            if name == "run_report" and any(c in banned for c in consts):
                bad.append(f"{filename}:{fn.name} run_report on {consts}")
            if name == "ids" and any(c in banned for c in consts) and fn.name not in _MAY_READ_EVERY_CASE:
                bad.append(f"{filename}:{fn.name} ids({consts})")
    return bad


def test_the_holdout_guard_flags_a_test_that_scores_the_holdout_or_every_case():
    for src in ('def test_x(g):\n    te.run_report(g, set_name="all", tier="t0", day="day0")\n',
                'def test_x(g):\n    te.run_report(g, set_name="holdout", tier="t1")\n',
                'def test_x(g):\n    te._market_implied(g, g.ids("all"))\n'):
        assert _holdout_violations(src), src
    assert _holdout_violations('def test_x(g):\n    te.run_report(g, set_name="dev", tier="t0")\n') == []


def test_no_triage_test_asserts_a_holdout_inclusive_number():
    """R-11. ``run_report`` and ``_market_implied`` may be called on the dev split only, in every triage test file.

    The holdout is reported once, by ``holdout --once``. A test that scored ``holdout`` or ``all`` would hand anyone
    tuning thresholds a number that includes it.
    """
    bad = [v for path in _test_files() for v in _holdout_violations(path.read_text(), path.name)]
    assert bad == []


def test_predictions_depend_on_inputs_only_never_on_labels(golden, tmp_path):
    base = te.predict(golden, tier="t0", day="day0", case_ids=golden.ids("all"))
    g = tmp_path / "g"
    shutil.copytree(te.GOLDEN_DIR, g)
    labels = json.loads((g / "labels.json").read_text())
    for v in labels.values():
        v["label_class"] = "unknown"
        v["label_factor"] = 99.0
    (g / "labels.json").write_text(json.dumps(labels, indent=1) + "\n")
    scrambled = te.load_golden(g, verify=False)
    assert te.predict(scrambled, tier="t0", day="day0", case_ids=scrambled.ids("all")) == base
    src = ast.parse(SCRIPT.read_text())
    fn = next(n for n in ast.walk(src) if isinstance(n, ast.FunctionDef) and n.name == "predict")
    names = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "labels" not in names


def test_dev_report_through_the_cli_has_every_headline_field(capsys):
    code, rep, _ = _json_run(capsys, "run", "--set", "dev", "--tier", "t0", "--day", "day0")
    assert code == 0 and rep["set"] == "dev" and rep["tier"] == "t0" and rep["day"] == "day0"
    assert rep["all"]["n_cases"] == 25 and rep["headline"]["n_cases"] == sum(
        1 for r in rep["cases"] if r["label_confidence"] != "low")
    for k in ("coverage", "precision_by_confidence", "dangerous_errors", "accuracy_abstain_wrong"):
        assert k in rep["all"]
    assert set(rep["all"]["precision_by_confidence"]) == {"high", "medium", "low"}
    assert rep["all"]["coverage"]["wilson95"] is not None
    assert rep["golden_version"] == 1 and rep["label_rev"] == 2 and len(rep["manifest_sha256"]) == 64
    assert rep["rules_version"] == "triage-rules/1"
    assert any("after seeing" in c for c in rep["caveats"])                   # the upper-bound warning travels with the numbers
    assert {r["case_id"] for r in rep["cases"]}.isdisjoint(set(GROUND_TRUTH))


def test_day5_cli_and_text_output(capsys):
    code, rep, _ = _json_run(capsys, "run", "--set", "dev", "--tier", "t0", "--day", "day5")
    assert code == 0 and rep["day"] == "day5"
    code, out, _ = _run(["run", "--set", "dev", "--tier", "t0", "--day", "day0"], capsys)
    assert code == 0 and "coverage" in out and "dangerous errors" in out


def test_run_refuses_holdout_and_all_so_tuning_cannot_peek(capsys):
    for s in ("holdout", "all"):
        code, out, err = _run(["run", "--set", s, "--tier", "t0", "--day", "day0", "--json"], capsys)
        assert code == 3 and out.strip() == "" and "dev split only" in err


def test_market_implied_band_reports_the_dev_demerger_gap_against_the_exchange_derived_label(golden):
    # The gap is market data against a label: it does not depend on the rules, so it gives no tuning signal.
    # Dev split only (R-11). ABFRL: 1/gross = 2.9933 against 268.95 / 98.00 = 2.7444 is +9.1% (R-14), not +10.2%.
    mi = te._market_implied(golden, golden.ids("dev"))
    assert mi["declared_band_pct"] == T.MARKET_IMPLIED_UNCERTAINTY_PCT
    (abfrl,) = [r for r in mi["cases"] if r["case_id"] == "ABFRL_2025-05-22"]
    assert abfrl["market_implied"] == 2.9933 and abfrl["label_factor"] == 2.7444
    assert abfrl["gap_pct"] == pytest.approx(9.07, abs=0.01) and abfrl["covered"] is True
    assert mi["n"] == len(mi["cases"]) and mi["n_covered"] == sum(1 for r in mi["cases"] if r["covered"])


def test_market_implied_gap_arithmetic_and_the_band_edge_on_a_stub_set():
    """Pure arithmetic on invented cases, so no holdout label is read: gap = (1/gross - label) / label."""
    cases = [{"case_id": f"S{i}", "gross_factor": g} for i, g in enumerate((0.5, 0.5, 0.5))]
    labels = {"S0": {"label_class": "demerger", "label_factor": 2.0},               # 1/gross = 2.0: gap 0
              "S1": {"label_class": "demerger", "label_factor": 1.8},               # +11.1%: inside the 12% band
              "S2": {"label_class": "scheme", "label_factor": 1.6}}                 # +25%: outside it
    stub = te.Golden(Path("."), cases, labels, {}, {}, "0" * 64)
    mi = te._market_implied(stub, ["S0", "S1", "S2"])
    assert [round(r["gap_pct"], 1) for r in mi["cases"]] == [0.0, 11.1, 25.0]
    assert [r["covered"] for r in mi["cases"]] == [True, True, False] and (mi["n"], mi["n_covered"]) == (3, 2)


# ------------------------------------------------------------------ T1: document fixtures (synthetic)

def test_t1_documents_are_all_marked_synthetic_and_not_point_in_time_true(golden):
    doc = golden.documents
    assert doc["synthetic"] is True and doc["pit_true"] is False
    assert tuple(doc["cases"]) == T1_CASES
    for cid, spec in doc["cases"].items():
        assert spec["documents"], cid
        for d in spec["documents"]:
            assert d["synthetic"] is True
            assert d.get("excerpt") is None or d["excerpt"].startswith("SYNTHETIC EXCERPT.")
            assert "body_text" not in d or d["body_text"].startswith("SYNTHETIC")


def test_t1_caveat_and_flags_travel_with_the_report(golden, no_network):
    rep = te.run_report(golden, set_name="dev", tier="t1", day="day0")
    assert rep["documents_synthetic"] is True and rep["pit_true"] is False
    assert any("never" in c and "live" in c for c in rep["caveats"])
    (row,) = rep["cases"]
    assert row["case_id"] == "ABFRL_2025-05-22" and row["synthetic_documents"] >= 1
    assert (row["predicted"], row["confidence"], row["rule_id"]) == ("demerger", "medium", "R2")
    assert rep["all"]["dangerous_errors"]["count"] == 0 and rep["gates"]["passed"] is True


_ADV_DEV_CASES = ("GPIL_2024-01-01", "CGCL_2024-01-01", "MOTILALOFS_2024-01-01", "INDIAMART_2019-10-27",
                  "INDIAMART_2019-10-29", "INDIAMART_2020-11-14", "INDIAMART_2020-11-17")


def _adv_item(n, **over):
    """A dated document the engine archived or an agent attached, for the day before the ex-date (synthetic)."""
    from quant.triage import evidence

    base = dict(schema="triage.envelope/1", provenance="yahoo_news", subject={"isin": "INE000000000", "ex_date": "2000-01-01"},
                url=None, title="SYNTHETIC headline naming the company", headline=None, published_at=None,
                published_at_basis="source", published_at_precision="date", source_class="news", relevance="entity",
                relevance_basis="token_match", excerpt=None, typed_fields={}, body_sha256=None, entity_match=True,
                source_status="ok")
    base.update(over)
    return evidence.EvidenceItem(f"TD-{n:016x}", f"{n:064x}", "2000-01-01T00:00:00.000000Z", base, True, "ok")


@pytest.mark.parametrize("day", ["day0", "day5"])
@pytest.mark.parametrize("cid", _ADV_DEV_CASES)
def test_a_dated_headline_or_attached_link_never_turns_a_golden_vendor_artifact_into_a_genuine_call(golden, cid, day):
    """F1: the golden set carries no news fixtures, so the zero-dangerous-errors gate could not see R7 outrank R5 and R6.
    Dev cases only (R-11)."""
    from quant.triage import rules

    case = golden.by_id[cid]
    f = te._recorded_features(case, golden.inputs, post_bars=day == "day5")
    f = dataclasses.replace(f, series="EQ", placeholder=False)                    # recorded rows know neither (as the T1 overrides)
    visible = rules.classify(f).rule_id in ("R5", "R6")
    # Where the artifact is visible (the cross-section, or the reversal pair) the peers are made to move: that is what
    # lifted the call to medium before the fix. Day 0 hides the forward half of a reversal pair, so there the peers
    # stay unknown, as recorded: the engine cannot see an artifact it has no bar for.
    f = dataclasses.replace(f, n_peers_moved=3 if visible else None)
    day_before = (date.fromisoformat(f.ex_date) - timedelta(days=1)).isoformat()
    docs = {
        "news": _adv_item(1, published_at=day_before),
        "agent_link": _adv_item(2, provenance="agent_attach", published_at_basis="agent_declared",
                                source_class="regulator", relevance="sector", entity_match=None, published_at=day_before),
        "human_regulator": _adv_item(3, provenance="human_attach", published_at_basis="human_declared",
                                     source_class="regulator", relevance="sector", entity_match=None, published_at=day_before),
    }
    base = rules.classify(f)
    for name, doc in docs.items():
        got = rules.classify(f, [doc])
        assert not (got.label == "genuine_event" and got.confidence != "low"), (cid, day, name, got.rule_id)
        if base.rule_id in ("R5", "R6"):
            assert (got.label, got.rule_id, got.confidence) == (base.label, base.rule_id, base.confidence), (cid, day, name)
        assert got.resolve_kind != "genuine" or got.confidence == "low"


def test_t1_factor_error_against_the_exchange_derived_label_is_inside_two_percent(golden):
    f = te.run_report(golden, set_name="dev", tier="t1", day="day0")["factor_check"]
    assert f["n"] == 1 and f["max_abs_error_pct"] < 0.01 and f["within_2pct"] == 1 and f["tolerance_pct"] == 2.0


def test_t1_dev_slice_through_the_cli(capsys, no_network):
    code, rep, _ = _json_run(capsys, "run", "--set", "dev", "--tier", "t1")
    assert code == 0 and rep["tier"] == "t1" and rep["day"] == "day0"
    assert [r["case_id"] for r in rep["cases"]] == ["ABFRL_2025-05-22"]
    assert rep["documents_synthetic"] is True
    tgt = rep["accuracy_target"]                                     # 1 of 1 is 100% and proves nothing (fix round 2, F2)
    assert tgt["met"] is not True and tgt["met"] is None and tgt["decidable"] is False
    assert (tgt["k"], tgt["n"], tgt["min_n"]) == (1, 1, te.ACCURACY_MIN_N) and tgt["point_estimate_meets_target"] is True
    assert tgt["wilson95"][0] < 0.9                                  # the lower bound sits far below the target
    code, out, _ = _run(["run", "--set", "dev", "--tier", "t1"], capsys)
    assert "n=1" in out and "undecidable" in out and "1/1" in out


def test_accuracy_target_is_decidable_only_with_enough_cases(golden):
    base = te.run_report(golden, set_name="dev", tier="t1", day="day0")
    assert te.accuracy_target_block(0, 0) == {"target": 0.9, "min_n": te.ACCURACY_MIN_N, "k": 0, "n": 0, "rate": None,
                                              "wilson95": None, "decidable": False, "point_estimate_meets_target": None,
                                              "met": None}
    assert te.accuracy_target_block(6, 7)["met"] is None and te.accuracy_target_block(6, 7)["point_estimate_meets_target"] is False
    n = te.ACCURACY_MIN_N
    low = int(n * 0.85)
    assert te.accuracy_target_block(n, n)["met"] is True and te.accuracy_target_block(low, n)["met"] is False
    assert te.accuracy_target_block(low, n)["decidable"] is True
    assert base["accuracy_target"] == te.accuracy_target_block(1, 1)


def test_t1_with_a_document_fixture_in_the_wrong_window_abstains(golden):
    """A genuine-event document dated before the R7 window is context, not evidence."""
    spec = copy.deepcopy(golden.documents)
    for d in spec["cases"]["IEX_2025-07-24"]["documents"]:
        d["published_at"] = "2025-06-01"
    g = copy.copy(golden)
    g.documents = spec
    rows = te.predict(g, tier="t1", day="day0", case_ids=["IEX_2025-07-24"])
    assert rows[0]["predicted"] == "unknown"


# ------------------------------------------------------------------ holdout, once

@pytest.fixture
def golden_copy(tmp_path):
    """A private copy of the golden directory: a holdout look in a test must never mark the real one."""
    g = tmp_path / "g"
    shutil.copytree(te.GOLDEN_DIR, g)
    return g


def test_holdout_needs_once_reports_exactly_the_holdout_and_refuses_a_second_look(golden_copy, capsys, no_network):
    marker = golden_copy / "holdout_reported.json"                               # always beside MANIFEST.json
    code, _, err = _run(["holdout", "--golden", str(golden_copy), "--json"], capsys)
    assert code == 1 and not marker.exists()
    code, rep, _ = _json_run(capsys, "holdout", "--once", "--golden", str(golden_copy))
    assert code == 0 and marker.exists()
    assert rep["set"] == "holdout" and set(rep["reports"]) == {"t0_day0", "t0_day5", "t1_day0"}
    assert len(rep["reports"]["t0_day0"]["cases"]) == 11
    assert set(GROUND_TRUTH) <= {r["case_id"] for r in rep["reports"]["t0_day0"]["cases"]}
    saved = json.loads(marker.read_text())
    assert saved["report_sha256"] == rep["report_sha256"] and saved["manifest_sha256"] == rep["manifest_sha256"]
    man = json.loads((golden_copy / "MANIFEST.json").read_text())
    assert man["holdout_report_sha256"] == rep["report_sha256"]                  # the manifest itself names the look
    te.load_golden(golden_copy)                                                   # and stays valid (files are unchanged)
    before = marker.read_bytes()
    code, out, err = _run(["holdout", "--once", "--golden", str(golden_copy), "--json"], capsys)
    assert code == 3 and out.strip() == "" and "already" in err and rep["report_sha256"][:12] in err
    assert marker.read_bytes() == before


def test_holdout_second_look_is_refused_when_the_manifest_names_a_report_even_without_a_marker(golden_copy, capsys):
    """A fresh checkout has the committed manifest; deleting or never having the marker file must not reopen the holdout."""
    man = json.loads((golden_copy / "MANIFEST.json").read_text())
    man["holdout_report_sha256"] = "ab" * 32
    (golden_copy / "MANIFEST.json").write_text(json.dumps(man, indent=1) + "\n")
    code, out, err = _run(["holdout", "--once", "--golden", str(golden_copy), "--json"], capsys)
    assert code == 3 and out.strip() == "" and "already" in err and "abababababab" in err
    assert not (golden_copy / "holdout_reported.json").exists()
    with pytest.raises(te.GoldenError, match="already"):
        te.holdout_report(te.load_golden(golden_copy))                            # the library function refuses too


def test_holdout_marker_path_cannot_be_redirected_to_get_a_second_look(golden_copy, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        te.main(["holdout", "--once", "--marker", str(tmp_path / "other.json"), "--golden", str(golden_copy), "--json"])
    err = capsys.readouterr().err
    assert exc.value.code == 1 and "--marker" in err                              # the option no longer exists
    assert not (tmp_path / "other.json").exists() and not (golden_copy / "holdout_reported.json").exists()
    _json_run(capsys, "holdout", "--once", "--golden", str(golden_copy))
    (golden_copy / "holdout_reported.json").unlink()                              # deleting the marker is not enough
    code, _, err = _run(["holdout", "--once", "--golden", str(golden_copy), "--json"], capsys)
    assert code == 3 and "already" in err


def test_a_manifest_rewrite_keeps_the_holdout_report_hash(golden_copy, capsys):
    _json_run(capsys, "holdout", "--once", "--golden", str(golden_copy))
    before = (golden_copy / "MANIFEST.json").read_bytes()
    te.write_manifest(golden_copy)
    assert (golden_copy / "MANIFEST.json").read_bytes() == before


# ------------------------------------------------------------------ score-llm: recorded runs through the real validator

@pytest.fixture(scope="module")
def replayed(golden):
    return te.replay_runs(te.RUNS_DIR, golden)


def test_the_recorded_runs_include_passing_and_failing_examples(replayed):
    rows = {r["case_id"]: r for r in replayed["runs"]}
    assert set(rows) == {"TMPV_2025-10-14", "POLICYBZR_2026-09-24", "VEDL_2026-04-30", "ABFRL_2025-05-22"}
    assert rows["TMPV_2025-10-14"]["validator_ok"] is True
    assert (rows["TMPV_2025-10-14"]["label"], rows["TMPV_2025-10-14"]["confidence"]) == ("demerger", "high")
    assert (rows["POLICYBZR_2026-09-24"]["label"], rows["POLICYBZR_2026-09-24"]["confidence"]) == ("genuine_event", "medium")
    vedl, abfrl = rows["VEDL_2026-04-30"], rows["ABFRL_2025-05-22"]
    assert vedl["validator_ok"] is False and any(e.startswith("V7:negative_check_failed") for e in vedl["errors"])
    assert abfrl["validator_ok"] is False
    assert any(e.startswith("V6:factor_mismatch") for e in abfrl["errors"]) and any(e.startswith("V4:quote_not_found") for e in abfrl["errors"])


def test_score_llm_metrics(replayed):
    m = replayed["metrics"]
    assert (m["validator_pass"]["k"], m["validator_pass"]["n"]) == (2, 4)
    assert m["dangerous_errors"]["count"] == 0                                  # nothing dangerous got through
    assert m["blocked_dangerous_attempts"]["count"] == 1                        # genuine on a demerger, stopped by V7
    assert m["blocked_dangerous_attempts"]["cases"] == [{"case_id": "VEDL_2026-04-30", "kind": "genuine_on_value_transfer"}]
    assert (m["agreement_with_engine"]["k"], m["agreement_with_engine"]["n"]) == (2, 2)
    assert m["added_value_on_engine_abstentions"] == {"k": 0, "n": 0, "rate": None, "wilson95": None}
    assert m["error_codes"]["V7:negative_check_failed"] == 1 and m["error_codes"]["V6:factor_mismatch"] == 1
    assert (m["accuracy"]["k"], m["accuracy"]["n"]) == (2, 2)                   # among proposals the validator accepted
    assert replayed["model_calls"] == 0 and replayed["synthetic_runs"] == 4


def test_recorded_runs_are_filed_by_model_and_prompt_hash_and_say_what_they_are(replayed):
    for r in replayed["runs"]:
        assert re.fullmatch(r"[0-9a-f]{64}", r["prompt_sha256"]) and r["model"] == "hand_written_example"
        assert r["path"].split("/")[-3] == "hand_written_example" and r["path"].split("/")[-2] == r["prompt_sha256"]
        assert r["synthetic"] is True
        assert r["prompt_matches_current"] is True
    for path in te.RUNS_DIR.rglob("*.json"):
        run = json.loads(path.read_text())
        assert run["schema"] == "triage.llm_run/1"
        assert run["proposal"]["producer"]["prompt_sha256"] == run["prompt_sha256"] == path.parent.name
        assert {"model", "temperature", "usage", "transcript", "case_id", "synthetic"} <= set(run)
        assert run["case_id"] == path.stem
        assert all(set(t) >= {"cmd", "exit"} for t in run["transcript"])


def test_score_llm_runs_without_a_model_or_the_network(golden, no_network):
    before = set(sys.modules)
    te.replay_runs(te.RUNS_DIR, golden)
    new = {m.split(".")[0] for m in set(sys.modules) - before}
    assert not new & {"anthropic", "openai", "httpx", "requests", "urllib3", "yfinance", "google"}


def _single_run_dir(tmp_path, case_id, mutate):
    src = next(te.RUNS_DIR.rglob(f"{case_id}.json"))
    run = json.loads(src.read_text())
    mutate(run)
    dst = tmp_path / "runs" / src.parent.parent.name / src.parent.name
    dst.mkdir(parents=True)
    (dst / src.name).write_text(json.dumps(run))
    return tmp_path / "runs"


def test_a_tampered_recorded_quote_is_caught_by_the_same_validator(golden, tmp_path):
    def mutate(run):
        run["proposal"]["citations"][0]["quote"] = "Previous close Rs 660.75. SPOS price Rs 401.00"
    out = te.replay_runs(_single_run_dir(tmp_path, "TMPV_2025-10-14", mutate), golden)
    (row,) = out["runs"]
    assert row["validator_ok"] is False and any(e.startswith("V4:quote_not_found") for e in row["errors"])


def test_a_recorded_run_bound_to_another_pack_is_refused(golden, tmp_path):
    def mutate(run):
        run["proposal"]["producer"]["pack_sha256"] = "0" * 64
    (row,) = te.replay_runs(_single_run_dir(tmp_path, "POLICYBZR_2026-09-24", mutate), golden)["runs"]
    assert row["validator_ok"] is False and any(e.startswith("V1:binding") for e in row["errors"])


def test_unknown_case_unreadable_file_and_empty_dir_are_reported_not_hidden(golden, tmp_path):
    d = tmp_path / "runs" / "m" / ("a" * 64)
    d.mkdir(parents=True)
    (d / "NOPE_2020-01-01.json").write_text(json.dumps({"schema": "triage.llm_run/1", "case_id": "NOPE_2020-01-01"}))
    (d / "TMPV_2025-10-14.json").write_text("{not json")
    out = te.replay_runs(tmp_path / "runs", golden)
    assert {(r["case_id"], r["status"]) for r in out["runs"]} == {("NOPE_2020-01-01", "unknown_case"), ("TMPV_2025-10-14", "unreadable")}
    assert out["metrics"]["validator_pass"] == {"k": 0, "n": 0, "rate": None, "wilson95": None}   # none replayed: unknown, not 0%
    empty = te.replay_runs(tmp_path / "none", golden)
    assert empty["runs"] == [] and empty["metrics"]["validator_pass"]["rate"] is None


# ---- recorded runs carry the pack the agent saw, and are validated against that pack (fix round 1, F1)

PRODUCTION_PACK_KEYS = {"schema", "rules_version", "subject", "as_of", "cutoff_utc", "price_store_vintage", "features",
                        "source_status", "rejected_evidence", "evidence", "evidence_omitted"}


def _runs():
    return {p.stem: json.loads(p.read_text()) for p in sorted(te.RUNS_DIR.rglob("*.json"))}


def test_recorded_runs_embed_a_production_shaped_pack_bound_to_the_proposal():
    for cid, run in _runs().items():
        pack = run["pack"]
        assert set(pack) == PRODUCTION_PACK_KEYS, cid                        # what 'triage show --json' prints
        assert pack["subject"]["key"] == run["proposal"]["key"] and pack["as_of"] == run["proposal"]["as_of"]
        assert triage_store.pack_bytes(pack)[0] == run["proposal"]["producer"]["pack_sha256"], cid


def test_replay_validates_against_the_embedded_pack_not_a_reduced_one(replayed):
    assert {r["pack_source"] for r in replayed["runs"]} == {"embedded"}


def test_a_run_bound_to_its_own_pack_passes_when_only_non_decisive_pack_fields_differ(golden, tmp_path):
    """A real pack has rejected evidence the replay world does not; that is not a feature or evidence difference."""
    def mutate(run):
        run["pack"]["evidence_omitted"] = 3
        run["pack"]["rejected_evidence"] = [{"capture_id": "TD-0000000000000000", "reason": "after_cutoff"}]
        run["proposal"]["producer"]["pack_sha256"] = triage_store.pack_bytes(run["pack"])[0]
    (row,) = te.replay_runs(_single_run_dir(tmp_path, "TMPV_2025-10-14", mutate), golden)["runs"]
    assert row["validator_ok"] is True and row["pack_source"] == "embedded" and row["status"] == "replayed"
    assert row["pack_matches_replay_world"] is True


def _rehashed_pack_edit(edit):
    """Edit the embedded pack, then re-hash it into producer.pack_sha256: the run is self-consistent."""
    def mutate(run):
        edit(run["pack"])
        run["proposal"]["producer"]["pack_sha256"] = triage_store.pack_bytes(run["pack"])[0]
    return mutate


def test_a_rehashed_pack_with_edited_features_is_a_problem_and_never_counts_as_a_pass(golden, tmp_path, capsys):
    """V1:binding only proves the run agrees with itself, and V5, V6 and V7 read the pack's features (fix round 2, F1)."""
    def edit(pack):
        pack["features"]["gross_factor"] = 0.5
    runs = _single_run_dir(tmp_path, "TMPV_2025-10-14", _rehashed_pack_edit(edit))
    out = te.replay_runs(runs, golden)
    (row,) = out["runs"]
    assert row["status"] == "pack_mismatch" and row["pack_matches_replay_world"] is False
    assert row["pack_mismatch"] == ["features"] and row["pack_mismatch_features"] == ["gross_factor"]
    assert out["problems"]["pack_mismatch"] == 1
    assert out["metrics"]["validator_pass"] == {"k": 0, "n": 0, "rate": None, "wilson95": None}   # kept out of the metrics
    assert out["metrics"]["accuracy"]["n"] == 0
    code, rep, _ = _json_run(capsys, "score-llm", "--runs", str(runs))
    assert code == 2 and rep["problems"]["pack_mismatch"] == 1
    code, text, _ = _run(["score-llm", "--runs", str(runs)], capsys)
    assert code == 2 and "pack mismatch 1" in text


def test_a_rehashed_pack_with_edited_evidence_is_a_problem_too(golden, tmp_path):
    def edit(pack):
        pack["evidence"] = []
    out = te.replay_runs(_single_run_dir(tmp_path, "TMPV_2025-10-14", _rehashed_pack_edit(edit)), golden)
    (row,) = out["runs"]
    assert row["status"] == "pack_mismatch" and row["pack_mismatch"] == ["evidence"]
    assert out["problems"]["pack_mismatch"] == 1 and out["metrics"]["validator_pass"]["n"] == 0


def test_allow_pack_mismatch_scores_the_run_but_reports_it_as_a_warning(golden, tmp_path, capsys):
    """For a real recorded run whose pack cannot be rebuilt from the fixture world: explicit, and still visible."""
    def edit(pack):
        pack["features"]["gross_factor"] = 0.5
    runs = _single_run_dir(tmp_path, "TMPV_2025-10-14", _rehashed_pack_edit(edit))
    out = te.replay_runs(runs, golden, allow_pack_mismatch=True)
    (row,) = out["runs"]
    assert row["status"] == "replayed" and row["pack_matches_replay_world"] is False and row["pack_mismatch_allowed"] is True
    assert out["problems"]["pack_mismatch"] == 0 and out["warnings"]["pack_mismatch_allowed"] == 1
    code, text, _ = _run(["score-llm", "--runs", str(runs), "--allow-pack-mismatch"], capsys)
    assert code in (0, 2) and "pack mismatch allowed 1" in text
    assert te.replay_runs(runs, golden)["warnings"]["pack_mismatch_allowed"] == 0   # not allowed by default


def test_a_pack_edited_after_the_hash_was_taken_is_refused(golden, tmp_path):
    def mutate(run):
        run["pack"]["features"]["gross_factor"] = 0.5
    (row,) = te.replay_runs(_single_run_dir(tmp_path, "TMPV_2025-10-14", mutate), golden)["runs"]
    assert row["validator_ok"] is False and any(e.startswith("V1:binding") for e in row["errors"])


def test_a_run_without_an_embedded_pack_falls_back_to_the_replay_world_and_says_so(golden, tmp_path):
    world = te.describe_world(golden, "POLICYBZR_2026-09-24")

    def mutate(run):
        del run["pack"]
        run["proposal"]["producer"]["pack_sha256"] = world["pack_sha256"]
    (row,) = te.replay_runs(_single_run_dir(tmp_path, "POLICYBZR_2026-09-24", mutate), golden)["runs"]
    assert row["pack_source"] == "replay_world" and row["validator_ok"] is True


def test_replay_world_pack_is_production_shaped_and_uses_the_rules_product_range(golden, monkeypatch):
    seen = {}
    real = te.feat.flat

    def spy(f, items=(), **kw):
        seen.update(kw)
        return real(f, items, **kw)
    monkeypatch.setattr(te.feat, "flat", spy)
    world = te.describe_world(golden, "TMPV_2025-10-14")
    assert seen == {"product_range": te.rules.R6_PRODUCT_RANGE}
    assert set(world["pack"]) == PRODUCTION_PACK_KEYS
    assert world["pack"]["source_status"]["price_store"] == "skipped"            # no price store ran: never "empty"


def test_rebuild_examples_rewrites_hashes_of_hand_written_runs_only(golden, tmp_path):
    work = tmp_path / "runs"
    shutil.copytree(te.RUNS_DIR, work)
    old_dir = next(work.glob("hand_written_example/*"))
    current_dir = old_dir.name
    old_dir = old_dir.rename(old_dir.with_name("1" * 64))                        # simulate a prompt edit: old hash, old dir
    for f in old_dir.glob("*.json"):                                             # and a flat() edit
        run = json.loads(f.read_text())
        run["prompt_sha256"] = run["proposal"]["producer"]["prompt_sha256"] = "1" * 64
        run["proposal"]["producer"]["pack_sha256"] = "2" * 64
        run["pack"]["features"]["n_peers"] = 99
        f.write_text(json.dumps(run))
    other = work / "some_model" / ("3" * 64)
    other.mkdir(parents=True)
    (other / "TMPV_2025-10-14.json").write_text(json.dumps({"schema": "triage.llm_run/1", "case_id": "TMPV_2025-10-14",
                                                              "model": "some_model", "synthetic": False, "proposal": {}}))
    before_other = (other / "TMPV_2025-10-14.json").read_bytes()
    broken = te.replay_runs(work, golden)
    assert {r["prompt_matches_current"] for r in broken["runs"] if r["model"] == "hand_written_example"} == {False}
    out = te.rebuild_examples(work, golden)
    assert sorted(out["rewritten"]) == sorted(["ABFRL_2025-05-22", "POLICYBZR_2026-09-24", "TMPV_2025-10-14", "VEDL_2026-04-30"])
    assert (other / "TMPV_2025-10-14.json").read_bytes() == before_other         # a real model's run is never touched
    fixed = {r["case_id"]: r for r in te.replay_runs(work, golden)["runs"] if r["model"] == "hand_written_example"}
    assert {c: r["validator_ok"] for c, r in fixed.items()} == {"TMPV_2025-10-14": True, "POLICYBZR_2026-09-24": True,
                                                                 "VEDL_2026-04-30": False, "ABFRL_2025-05-22": False}
    assert all(r["prompt_matches_current"] for r in fixed.values())
    assert not old_dir.exists()                                                  # filed under the new prompt hash
    for p in te.RUNS_DIR.rglob("*.json"):                                        # and lands byte for byte on the committed files
        assert (work / p.relative_to(te.RUNS_DIR)).read_bytes() == p.read_bytes(), p.name
    assert sorted(p.name for p in (work / "hand_written_example" / current_dir).glob("*.json")) == sorted(
        f"{c}.json" for c in fixed)


def test_a_run_recorded_under_an_old_prompt_is_a_warning_and_fails_only_on_request(golden, tmp_path, capsys):
    def mutate(run):
        run["prompt_sha256"] = "1" * 64
    runs = _single_run_dir(tmp_path, "TMPV_2025-10-14", mutate)
    out = te.replay_runs(runs, golden)
    assert out["runs"][0]["prompt_matches_current"] is False and out["warnings"]["stale_prompt"] == 1
    assert out["problems"]["stale_prompt"] == 0
    code, text, _ = _run(["score-llm", "--runs", str(runs)], capsys)
    assert code == 0 and "stale prompt 1" in text                                 # shown, not silent
    code, rep, _ = _json_run(capsys, "score-llm", "--runs", str(runs), "--fail-on-stale-prompt")
    assert code == 2 and rep["problems"]["stale_prompt"] == 1


def test_hand_written_transcripts_label_their_stderr_line_as_illustrative():
    for run in _runs().values():
        for t in run["transcript"]:
            assert "stderr_first_line" not in t or t.get("illustrative") is True


def test_score_llm_cli(capsys, golden):
    code, rep, _ = _json_run(capsys, "score-llm", "--runs", str(te.RUNS_DIR))
    assert code == 0 and rep["metrics"]["validator_pass"]["n"] == 4
    code, out, err = _run(["score-llm", "--runs", str(te.RUNS_DIR / "missing"), "--json"], capsys)
    assert code == 3


def test_score_llm_exits_2_when_nothing_was_replayed_or_a_file_is_unreadable_or_names_an_unknown_case(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    code, rep, _ = _json_run(capsys, "score-llm", "--runs", str(empty))
    assert code == 2 and rep["metrics"]["validator_pass"]["n"] == 0            # nothing verified is not a pass
    d = tmp_path / "bad" / "m" / ("a" * 64)
    d.mkdir(parents=True)
    (d / "TMPV_2025-10-14.json").write_text("{not json")
    code, rep, _ = _json_run(capsys, "score-llm", "--runs", str(tmp_path / "bad"))
    assert code == 2 and rep["problems"] == {"unreadable": 1, "unknown_case": 0, "pack_mismatch": 0, "stale_prompt": 0}
    good = _single_run_dir(tmp_path / "g", "TMPV_2025-10-14", lambda run: None)
    assert _json_run(capsys, "score-llm", "--runs", str(good))[0] == 0
    (good / "hand_written_example" / ("b" * 64)).mkdir()
    (good / "hand_written_example" / ("b" * 64) / "NOPE_2020-01-01.json").write_text(
        json.dumps({"schema": "triage.llm_run/1", "case_id": "NOPE_2020-01-01"}))
    code, rep, _ = _json_run(capsys, "score-llm", "--runs", str(good))
    assert code == 2 and rep["problems"] == {"unreadable": 0, "unknown_case": 1, "pack_mismatch": 0, "stale_prompt": 0}


def test_a_dangerous_error_makes_run_exit_2_and_fail_the_gate(monkeypatch, capsys, golden):
    real = te.rules.classify
    genuine = {cid for cid, v in golden.labels.items() if v["label_class"] == "genuine_event"}
    by_key = {(c["isin"], c["ex_date"]): c["case_id"] for c in golden.inputs}

    def flipped(f, *a, **k):
        d = real(f, *a, **k)
        if by_key.get((f.isin, f.ex_date)) in genuine:
            return dataclasses.replace(d, label="demerger", confidence="medium", rule_id="R2", action="resolve",
                                       resolve_kind="demerger", factor=0.9)
        return d
    monkeypatch.setattr(te.rules, "classify", flipped)
    rep = te.run_report(golden, set_name="dev", tier="t0", day="day0")
    assert rep["gates"]["passed"] is False and rep["gates"]["dangerous_errors"] >= 1
    assert all(c["kind"] == "value_transfer_on_non_transfer" for c in rep["all"]["dangerous_errors"]["cases"])
    code, out, err = _run(["run", "--set", "dev", "--tier", "t0", "--day", "day0"], capsys)
    assert code == 2 and "FAILED" in out
    code, rep, _ = _json_run(capsys, "run", "--set", "dev", "--tier", "t0", "--day", "day0")
    assert code == 2 and rep["gates"]["passed"] is False


def test_a_spurious_adjustment_under_another_label_makes_run_exit_2(monkeypatch, capsys, golden):
    real = te.rules.classify
    genuine = {cid for cid, v in golden.labels.items() if v["label_class"] == "genuine_event"}
    by_key = {(c["isin"], c["ex_date"]): c["case_id"] for c in golden.inputs}

    def flipped(f, *a, **k):
        d = real(f, *a, **k)
        if by_key.get((f.isin, f.ex_date)) in genuine:                 # a vendor-ratio factor under a non-transfer label
            return dataclasses.replace(d, label="vendor_error_other", confidence="medium", rule_id="R4", action="resolve",
                                       resolve_kind="manual_adj", factor=0.9)
        return d
    monkeypatch.setattr(te.rules, "classify", flipped)
    rep = te.run_report(golden, set_name="dev", tier="t0", day="day0")
    assert rep["gates"]["passed"] is False
    assert {c["kind"] for c in rep["all"]["dangerous_errors"]["cases"]} == {"spurious_adjustment"}
    code, out, _ = _run(["run", "--set", "dev", "--tier", "t0", "--day", "day0"], capsys)
    assert code == 2 and "FAILED" in out


def test_holdout_exits_2_when_any_holdout_report_has_a_dangerous_error(monkeypatch, capsys, golden_copy):
    real = te.rules.classify

    def flipped(f, *a, **k):
        return dataclasses.replace(real(f, *a, **k), label="demerger", confidence="high", rule_id="R2", action="resolve",
                                   resolve_kind="demerger", factor=0.9)
    monkeypatch.setattr(te.rules, "classify", flipped)
    code, rep, _ = _json_run(capsys, "holdout", "--once", "--golden", str(golden_copy))
    assert code == 2 and (golden_copy / "holdout_reported.json").exists()


def test_a_dangerous_recorded_run_that_the_validator_lets_through_makes_score_llm_exit_2(monkeypatch, capsys, tmp_path):
    runs = _single_run_dir(tmp_path, "POLICYBZR_2026-09-24", lambda run: run["proposal"]["classification"].update(
        label="demerger", confidence="medium"))

    def lenient(proposal, **kw):                          # a validator bug: the dangerous proposal is accepted
        return te.validate.ValidationResult(True, [], proposal)
    monkeypatch.setattr(te.validate, "validate", lenient)
    code, rep, _ = _json_run(capsys, "score-llm", "--runs", str(runs))
    assert code == 2 and rep["metrics"]["dangerous_errors"]["cases"] == [
        {"case_id": "POLICYBZR_2026-09-24", "kind": "value_transfer_on_non_transfer"}]


def test_a_spurious_adjustment_recorded_run_that_the_validator_lets_through_makes_score_llm_exit_2(monkeypatch, capsys, tmp_path):
    def mutate(run):
        run["proposal"]["classification"].update(label="vendor_error_other", confidence="medium")
        run["proposal"]["resolution"].update(action="resolve", resolve_kind="manual_adj", factor=0.9)
    runs = _single_run_dir(tmp_path, "POLICYBZR_2026-09-24", mutate)
    monkeypatch.setattr(te.validate, "validate", lambda proposal, **kw: te.validate.ValidationResult(True, [], proposal))
    code, rep, _ = _json_run(capsys, "score-llm", "--runs", str(runs))
    assert code == 2 and rep["metrics"]["dangerous_errors"]["cases"] == [
        {"case_id": "POLICYBZR_2026-09-24", "kind": "spurious_adjustment"}]


# ------------------------------------------------------------------ live-report: a read-only copy

AS_OF = "2026-10-30"
CREATED = "2026-10-30T10:00:00.000000Z"


def _engine_proposal(key, label, confidence, resolve_kind, factor, rule_id="R7"):
    return {
        "schema": "triage.proposal/1", "key": key, "as_of": AS_OF, "anomaly_kind": "suspected_jump",
        "producer": {"kind": "engine", "name": "rules:triage-rules/1", "prompt_sha256": None,
                     "pack_sha256": "a" * 64, "self_confidence": None},
        "classification": {"label": label, "subtype": None, "confidence": confidence, "rule_id": rule_id, "alternatives": []},
        "resolution": {"action": "resolve" if resolve_kind else "none", "resolve_kind": resolve_kind, "factor": factor,
                       "factor_basis": None, "factor_uncertainty_pct": None, "needs_force": False, "needs_primary": False,
                       "recheck_on": None, "factor_inputs": []},
        "claims": [], "citations": [], "negative_checks": [], "missing_evidence": [], "needs_human": [],
        "usage": {"n_fetches": 0, "tokens_in": None, "tokens_out": None, "wall_s": 0.0},
    }


def _llm_proposal(key, label, confidence, resolve_kind, factor):
    p = _engine_proposal(key, label, confidence, resolve_kind, factor, None)
    p["producer"] = {"kind": "llm", "name": "llm:test-model", "prompt_sha256": "b" * 64, "pack_sha256": "a" * 64,
                     "self_confidence": "high"}
    return p


def _insert_llm(ctx, proposal, n):
    """An llm row inserted as store.write_proposal would (R-5 makes write_proposal run the validator, which a bare
    fixture cannot satisfy; these rows exist to be measured, not accepted)."""
    from quant.db.core import canonical_json
    isin, ex = T.split_key(proposal["key"])
    pid = f"PT-{AS_OF}-9{n:02d}"
    ctx.conn.execute(
        "INSERT INTO proposals (proposal_id, created_run_id, as_of, kind, subject_id, payload_json, evidence_json, "
        "rule_id, proposed_by, llm_review, status, md_path) VALUES (?, ?, ?, 'other', ?, ?, '[]', ?, ?, NULL, "
        "'proposed', ?)",
        (pid, ctx.run_id, AS_OF, triage_store.subject_id(isin, ex), canonical_json(proposal), T.RULE_ID_AGENT,
         proposal["producer"]["name"], f"data/triage/sheets/{pid}.md"))
    return pid


def _decide(ctx, pid, isin, outcome, when, final, status="approved"):
    """A human resolution bound to ``pid``. ``final`` is the stored (kind, factor), as actions.resolve writes it."""
    ctx.conn.execute(
        "INSERT INTO decisions (decision_id, proposal_id, kind, tier, subject_id, title, context, options_json, decision, "
        "evidence_refs_json, decided_on, decided_by, approver_kind, status, effective_from, adr_path, git_sha) "
        "VALUES (?, ?, 'data_fix', 1, ?, 't', ?, ?, 'd', '[]', ?, 'human:test', 'human', 'approved', ?, 'x.md', 'g')",
        (f"D-{pid}", pid, isin,
         f"Suspected corporate action. Evidence-triage proposal {pid} (confirm 12345678): {outcome}",
         json.dumps([{"kind": final[0], "factor": final[1]}]), when, AS_OF))
    update_control(ctx, "proposals", {"proposal_id": pid},
                   {"status": status, "decided_on": when, "decided_by": "human:test", "decision_id": f"D-{pid}"})


GENUINE = ("manual_adj", 1.0)               # what 'genuine' is stored as


@pytest.fixture(scope="module")
def live_db(tmp_path_factory):
    """Built once for the module (every test only reads it, and live_report opens it read-only).

    Seven anomalies (INE000000001..7), each with an engine proposal; five also have an llm proposal.

    1 both say genuine; the human accepts the engine's.     2 engine only; the human modifies the factor.
    3 engine abstains, llm says demerger; human accepts the llm's.   4 both abstain; human resolves (no call).
    5 engine says demerger at medium, llm says genuine; human rejects the engine's and accepts the llm's.
    6 engine low, unresolved.                               7 engine abstains, llm says genuine, unresolved.
    """
    tmp_path = tmp_path_factory.mktemp("live")
    db = tmp_path / "live.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    cfg = load_config().with_paths(db=db, prices_db=tmp_path / "p.db", data_dir=tmp_path / "d", archive_dir=tmp_path / "a",
                                   knowledge_dir=tmp_path / "k", ui_dir=tmp_path / "u")
    key = {i: f"INE00000000{i}@2026-10-2{i}" for i in range(1, 8)}
    ids = {}
    with RunContext(as_of=AS_OF, kind="triage", track="live", cfg=cfg, clock=FrozenClock(CREATED),
                    actor=Actor(kind="system", name="t")) as ctx:
        for n, args in {1: ("genuine_event", "medium", "genuine", 1.0), 2: ("demerger", "high", "demerger", 1.6519, "R2"),
                        3: ("unknown", "low", None, None, None), 4: ("unknown", "low", None, None, None),
                        5: ("demerger", "medium", "demerger", 2.0, "R3"), 6: ("vendor_error_other", "low", None, None, "R6"),
                        7: ("unknown", "low", None, None, None)}.items():
            ids[f"e{n}"] = triage_store.write_proposal(ctx, _engine_proposal(key[n], *args), []).proposal_id
        ids["l1"] = _insert_llm(ctx, _llm_proposal(key[1], "genuine_event", "medium", "genuine", 1.0), 1)
        ids["l3"] = _insert_llm(ctx, _llm_proposal(key[3], "demerger", "medium", "demerger", 2.0), 3)
        ids["l4"] = _insert_llm(ctx, _llm_proposal(key[4], "unknown", "low", None, None), 4)
        ids["l5"] = _insert_llm(ctx, _llm_proposal(key[5], "genuine_event", "low", "genuine", 1.0), 5)
        ids["l7"] = _insert_llm(ctx, _llm_proposal(key[7], "genuine_event", "low", "genuine", 1.0), 7)
    with RunContext(as_of=AS_OF, kind="decide", track="live", cfg=cfg, clock=FrozenClock("2026-10-30T10:30:00.000000Z"),
                    actor=Actor(kind="human", name="test")) as ctx:
        _decide(ctx, ids["e1"], "INE000000001", "accepted", "2026-10-30T10:30:00.000000Z", GENUINE)
        _decide(ctx, ids["e2"], "INE000000002", "modified (factor 1.6519 -> 1.7)", "2026-10-30T12:00:00.000000Z",
                ("demerger", 1.7))
        _decide(ctx, ids["l3"], "INE000000003", "accepted", "2026-10-30T10:40:00.000000Z", ("demerger", 2.0))
        _decide(ctx, ids["e4"], "INE000000004", "no_call (no resolve call proposed; operator chose genuine factor 1)",
                "2026-10-30T10:45:00.000000Z", GENUINE)
        update_control(ctx, "proposals", {"proposal_id": ids["e5"]},
                       {"status": "rejected", "decided_on": "2026-10-30T11:00:00.000000Z", "decided_by": "human:test",
                        "llm_review": "REJECT_wrong_label"})
        _decide(ctx, ids["l5"], "INE000000005", "accepted", "2026-10-30T11:00:00.000000Z", GENUINE)
    return db, ids


def _block(rep, who):
    return rep["by_producer"][who]


def test_live_report_splits_every_metric_per_producer_and_pools_nothing(live_db):
    """R-12: engine and llm each get their own outcomes, rates, dangerous errors and review time."""
    db, ids = live_db
    rep = te.live_report(db)
    assert rep["status"] == "ok" and rep["tier"] == "t2_live" and rep["advisory"] is True
    assert set(rep["by_producer"]) == {"engine", "llm"}
    for pooled in ("outcomes", "anchored_agreement", "review_minutes", "coverage_non_abstain", "rejection_reasons",
                   "claim_status", "proposals"):
        assert pooled not in rep, pooled                                       # a pooled number would hide who earned it
    eng, llm = _block(rep, "engine"), _block(rep, "llm")
    assert (eng["n"], llm["n"]) == (7, 5)
    assert eng["by_label"] == {"genuine_event": 1, "demerger": 2, "unknown": 3, "vendor_error_other": 1}
    assert llm["by_label"] == {"genuine_event": 3, "demerger": 1, "unknown": 1}
    assert eng["by_confidence"] == {"medium": 2, "high": 1, "low": 4} and llm["by_confidence"] == {"medium": 2, "low": 3}
    assert eng["outcomes"] == {"accepted": 1, "modified": 1, "rejected": 1, "no_call": 1, "pending": 2, "overtaken": 1}
    assert llm["outcomes"] == {"accepted": 2, "modified": 0, "rejected": 0, "no_call": 0, "pending": 1, "overtaken": 2}
    assert eng["rejection_reasons"] == {"wrong_label": 1} and llm["rejection_reasons"] == {}
    assert rep["superseded"] == 0


def test_no_call_is_a_third_outcome_kept_out_of_the_accept_and_override_rates(live_db):
    """R-1: the engine's no_call (a human resolved an abstention) is neither an acceptance nor an override."""
    db, _ = live_db
    eng, llm = _block(te.live_report(db), "engine"), _block(te.live_report(db), "llm")
    assert eng["outcomes"]["no_call"] == 1
    assert (eng["accept_rate"]["k"], eng["accept_rate"]["n"]) == (1, 3)         # accepted, modified, rejected
    assert (eng["override_rate"]["k"], eng["override_rate"]["n"]) == (2, 3)     # modified and rejected; no_call is not in n
    assert eng["accept_rate"]["rate"] == pytest.approx(1 / 3) and eng["override_rate"]["rate"] == pytest.approx(2 / 3)
    assert (llm["accept_rate"]["k"], llm["accept_rate"]["n"]) == (2, 2) and llm["override_rate"]["k"] == 0
    assert eng["claim_status"] == {"decided_calls": 3, "live_n_needed": 27, "enough_for_claim": False}
    assert any("no_call" in c for c in te.live_report(db)["caveats"])


def test_a_proposal_overtaken_by_the_other_producers_resolution_is_not_pending(live_db):
    """The key was resolved through the other producer's proposal: the sibling is closed, not waiting."""
    db, _ = live_db
    rep = te.live_report(db)
    assert _block(rep, "engine")["outcomes"]["overtaken"] == 1 and _block(rep, "engine")["outcomes"]["pending"] == 2
    assert _block(rep, "llm")["outcomes"]["overtaken"] == 2 and _block(rep, "llm")["outcomes"]["pending"] == 1


def test_dangerous_errors_are_scored_per_producer_against_the_humans_final_resolution(live_db):
    db, ids = live_db
    rep = te.live_report(db)
    eng, llm = _block(rep, "engine")["dangerous_errors"], _block(rep, "llm")["dangerous_errors"]
    assert eng["count"] == 1 and eng["cases"] == [{"proposal_id": ids["e5"], "key": "INE000000005@2026-10-25",
                                                    "kind": "value_transfer_on_non_transfer"}]
    assert (eng["scored"], eng["unresolved"]) == (3, 1)      # abstentions are not scored; INE...6 has no resolution yet
    assert llm["count"] == 0 and (llm["scored"], llm["unresolved"]) == (3, 1)
    assert any("anchored" in c for c in rep["caveats"])


def test_added_value_on_engine_abstentions_belongs_to_the_llm_only(live_db):
    db, _ = live_db
    rep = te.live_report(db)
    eng = _block(rep, "engine")["added_value_on_engine_abstained"]
    assert eng["applicable"] is False and "reason" in eng                       # the engine cannot add value on its own abstentions
    av = _block(rep, "llm")["added_value_on_engine_abstained"]
    assert av["applicable"] is True
    assert (av["engine_abstained_keys"], av["llm_called"], av["resolved"], av["unresolved"]) == (3, 2, 1, 1)
    assert (av["agreed_with_human"]["k"], av["agreed_with_human"]["n"]) == (1, 1)     # INE...3, resolved as the llm proposed


def test_coverage_and_review_time_are_per_producer(live_db):
    db, _ = live_db
    rep = te.live_report(db)
    eng, llm = _block(rep, "engine"), _block(rep, "llm")
    assert (eng["coverage_non_abstain"]["k"], eng["coverage_non_abstain"]["n"]) == (4, 7)
    assert (llm["coverage_non_abstain"]["k"], llm["coverage_non_abstain"]["n"]) == (4, 5)
    assert eng["review_minutes"] == {"n": 4, "min": 30.0, "median": 52.5, "max": 120.0}   # decision minus proposal creation
    assert llm["review_minutes"] == {"n": 2, "min": 40.0, "median": 50.0, "max": 60.0}


def test_the_vs_final_outcome_matches_the_outcome_actions_resolve_recorded(live_db):
    """``outcome_vs_final`` compares any proposal with a final resolution the way actions.resolve compares the one
    the human bound; on the bound proposals the two agree (so the two cannot drift apart unnoticed)."""
    from quant.data.actions import TRIAGE_FACTOR_TOLERANCE
    assert te.FACTOR_TOLERANCE == TRIAGE_FACTOR_TOLERANCE
    db, ids = live_db
    conn = te.open_readonly(db)
    try:
        for name in ("e1", "e2", "l3", "e4", "l5"):
            payload, options, context = conn.execute(
                "SELECT p.payload_json, d.options_json, d.context FROM proposals p JOIN decisions d "
                "ON d.decision_id = p.decision_id WHERE p.proposal_id = ?", (ids[name],)).fetchone()
            final = json.loads(options)[0]
            recorded = re.search(r"\): (accepted|modified|no_call)", context).group(1)
            assert te.outcome_vs_final(json.loads(payload), final["kind"], final["factor"]) == recorded, name
    finally:
        conn.close()
    assert te.outcome_vs_final({"resolution": {"resolve_kind": "demerger", "factor": 2.0}}, "demerger", 2.03) == "accepted"
    assert te.outcome_vs_final({"resolution": {"resolve_kind": "demerger", "factor": 2.0}}, "demerger", 2.05) == "modified"
    assert te.outcome_vs_final({"resolution": {"resolve_kind": "demerger", "factor": 2.0}}, "scheme", 2.0) == "modified"
    assert te.outcome_vs_final({"resolution": {"resolve_kind": None}}, "manual_adj", 1.0) == "no_call"
    assert te.outcome_vs_final({"resolution": {"resolve_kind": "genuine", "factor": None}}, "manual_adj", 1.0) == "accepted"


def test_live_report_reads_a_ro_uri_and_never_writes(live_db):
    db, _ = live_db
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    listing = sorted(p.name for p in db.parent.iterdir())
    assert _block(te.live_report(f"file:{db}?mode=ro"), "engine")["outcomes"]["accepted"] == 1
    te.live_report(str(db))
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before
    assert sorted(p.name for p in db.parent.iterdir()) == listing               # no -wal or -shm sidecar left behind
    conn = te.open_readonly(str(db))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE proposals SET status = 'approved'")
    conn.close()
    with pytest.raises(sqlite3.OperationalError):
        te.open_readonly(f"file:{db}?mode=ro").execute("DELETE FROM proposals")


def test_live_report_refuses_a_writable_uri_and_a_missing_file(live_db, tmp_path):
    db, _ = live_db
    for bad in (f"file:{db}", f"file:{db}?mode=rw", f"file:{db}?mode=rwc", str(tmp_path / "missing.db"),
                f"file:{db}?mode=rw&mode=ro", f"file:{db}?mode=ro&mode=rw",          # a repeated mode is ambiguous
                f"file:{db}?mode=ro&immutable=0&mode=ro"):
        with pytest.raises(te.RefusedDb):
            te.live_report(bad)


def test_live_report_on_an_empty_database_says_unknown_not_zero(tmp_path):
    db = tmp_path / "empty.db"
    conn = connect(db)
    apply_schema(conn, kind="state")
    conn.close()
    rep = te.live_report(db)
    assert rep["status"] == "no_triage_proposals"
    for who in ("engine", "llm"):
        b = _block(rep, who)
        assert b["n"] == 0 and b["accept_rate"]["rate"] is None and b["override_rate"]["rate"] is None
        assert b["coverage_non_abstain"]["rate"] is None
        assert b["review_minutes"] == {"n": 0, "min": None, "median": None, "max": None}
        assert b["dangerous_errors"]["count"] == 0 and b["dangerous_errors"]["scored"] == 0
    av = _block(rep, "llm")["added_value_on_engine_abstained"]
    assert av["agreed_with_human"]["rate"] is None and av["engine_abstained_keys"] == 0


def test_live_report_cli(live_db, capsys):
    db, _ = live_db
    code, rep, _ = _json_run(capsys, "live-report", "--db", str(db))
    assert code == 0 and _block(rep, "engine")["outcomes"]["accepted"] == 1
    code, out, _ = _run(["live-report", "--db", str(db)], capsys)
    assert code == 0 and "engine" in out and "llm" in out and "no_call" in out
    code, out, err = _run(["live-report", "--db", str(db.parent / "nope.db"), "--json"], capsys)
    assert code == 3 and out.strip() == ""


# ------------------------------------------------------------------ manifest and tamper exits through main()

def test_manifest_check_and_write_through_main(tmp_path, capsys):
    code, rep, _ = _json_run(capsys, "manifest", "--check")
    assert code == 0 and rep == {"ok": True, "golden_version": 1, "label_rev": 2}
    g = tmp_path / "g"
    shutil.copytree(te.GOLDEN_DIR, g)
    before = (g / "MANIFEST.json").read_bytes()
    code, rep, _ = _json_run(capsys, "manifest", "--write", "--golden", str(g))
    assert code == 0 and rep["n_cases"] == 36 and (g / "MANIFEST.json").read_bytes() == before
    code, rep, _ = _json_run(capsys, "manifest", "--check", "--golden", str(g))
    assert code == 0


def test_a_tampered_golden_set_is_refused_with_exit_3_by_every_command(tmp_path, capsys):
    g = tmp_path / "g"
    shutil.copytree(te.GOLDEN_DIR, g)
    labels = json.loads((g / "labels.json").read_text())
    labels["TMPV_2025-10-14"]["label_class"] = "genuine_event"
    (g / "labels.json").write_text(json.dumps(labels))
    for argv in (["run", "--set", "dev", "--tier", "t0"], ["manifest", "--check"], ["score-llm"],
                 ["holdout", "--once"]):
        code, out, err = _run([*argv, "--golden", str(g), "--json"], capsys)
        assert code == 3 and out.strip() == "" and "sha256 differs" in err, argv
    assert not (g / "holdout_reported.json").exists()                             # a refused holdout leaves no marker
    code, out, err = _run(["manifest", "--write", "--golden", str(g), "--json"], capsys)
    assert code == 3 and "label_rev" in err                                       # a relabel needs a higher label_rev


# ------------------------------------------------------------------ the contract, the schema and the validator agree

CLOSED = {
    "labels": T.LABELS, "confidences": T.CONFIDENCES, "actions": T.ACTIONS, "resolve_kinds": T.RESOLVE_KINDS,
    "factor_bases": T.FACTOR_BASES, "anomaly_kinds": T.ANOMALY_KINDS, "claim_types": T.CLAIM_TYPES,
    "negative_check_results": T.NEGATIVE_RESULTS, "factor_input_names": T.FACTOR_INPUT_NAMES,
    "source_classes": T.SOURCE_CLASSES, "relevance": T.RELEVANCE,
}


def _validator_producer_kinds():
    """The validator keeps this set inline, so read it from the refusal it prints for an unknown kind."""
    prop = copy.deepcopy(_recorded_proposals()[0])
    prop["producer"]["kind"] = "zzz"
    (msg,) = [m for m in T.check_schema(prop, producer_kind="llm") if m.startswith("V1:schema:producer.kind: 'zzz'")]
    return tuple(ast.literal_eval(msg.split(" not in ", 1)[1]))


def _contract_sets():
    out = {}
    for line in CONTRACT.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 2 and cells[0].strip("`") in {*CLOSED, "producer_kinds"}:
            out[cells[0].strip("`")] = re.findall(r"`([^`]+)`", cells[1])
    return out


def test_contract_closed_sets_equal_the_validators():
    found = _contract_sets()
    assert set(found) == set(CLOSED) | {"producer_kinds"}
    for name, values in CLOSED.items():
        assert found[name] == list(values), name                                # same members, same order
    assert found["producer_kinds"] == list(_validator_producer_kinds()) == ["engine", "llm"]


def test_contract_top_level_field_names_equal_the_validators():
    text = CONTRACT.read_text()
    line = next(l for l in text.splitlines() if "Top-level keys:" in l)
    required, optional = re.search(r"Top-level keys: (.*?) \(required\) and (.*?) \(optional\)", line).groups()
    assert re.findall(r"`([^`]+)`", required) == list(T._TOP_REQUIRED)
    assert re.findall(r"`([^`]+)`", optional) == list(T._TOP_OPTIONAL)


def test_contract_numbers_equal_the_validators():
    """The contract's own caps are prose; pull each one out of the text and compare it with the validator (F5)."""
    text = CONTRACT.read_text()

    def one(pattern):
        found = re.findall(pattern, text)
        assert found, pattern
        return {int(x) for x in found}
    assert one(r"quote is at most (\d+) words") == {T.MAX_QUOTE_WORDS}
    assert one(r"Each quote is at most (\d+) words") == {T.MAX_QUOTE_WORDS}
    assert one(r"`--excerpt` \(at most (\d+) words\)") == {T.MAX_EXCERPT_WORDS}
    assert one(r"pack \(at most (\d+) KB\)") == {T.MAX_PACK_BYTES // 1024}
    m = re.search(r"payload at most (\d+) KB, cited evidence list at most (\d+) KB, pack at most (\d+) KB", text)
    assert m and tuple(int(x) * 1024 for x in m.groups()) == (T.MAX_PAYLOAD_BYTES, T.MAX_EVIDENCE_JSON_BYTES, T.MAX_PACK_BYTES)
    (limit,) = one(r"\(at most (\d+) characters\)")                            # producer.name, inline in the validator
    prop = copy.deepcopy(_recorded_proposals()[0])
    prop["producer"]["name"] = "llm:" + "x" * (limit - 4)
    assert not [m for m in T.check_schema(prop, producer_kind="llm") if "producer.name" in m]
    prop["producer"]["name"] += "x"
    assert [m for m in T.check_schema(prop, producer_kind="llm") if "producer.name" in m]


def _schema():
    return json.loads(SCHEMA_FILE.read_text())


def _deref(schema, node):
    if "$ref" in node:
        cur = schema
        for part in node["$ref"].lstrip("#/").split("/"):
            cur = cur[part]
        return cur
    return node


def _enum(schema, *path):
    node = schema
    for p in path:
        node = _deref(schema, node)
        node = node["items"] if p == "[]" else node["properties"][p]
    return [v for v in _deref(schema, node)["enum"] if v is not None]


def test_schema_enums_equal_the_validators():
    s = _schema()
    assert _enum(s, "producer", "kind") == ["engine", "llm"]
    assert _enum(s, "anomaly_kind") == list(T.ANOMALY_KINDS)
    assert _enum(s, "classification", "label") == list(T.LABELS)
    assert _enum(s, "classification", "confidence") == list(T.CONFIDENCES)
    assert _enum(s, "classification", "alternatives", "[]", "label") == list(T.LABELS)
    assert _enum(s, "resolution", "action") == list(T.ACTIONS)
    assert _enum(s, "resolution", "resolve_kind") == list(T.RESOLVE_KINDS)
    assert _enum(s, "resolution", "factor_basis") == list(T.FACTOR_BASES)
    assert _enum(s, "resolution", "factor_inputs", "[]", "name") == list(T.FACTOR_INPUT_NAMES)
    assert _enum(s, "claims", "[]", "type") == list(T.CLAIM_TYPES)
    assert _enum(s, "negative_checks", "[]", "result") == list(T.NEGATIVE_RESULTS)
    assert s["properties"]["schema"]["const"] == T.SCHEMA_PROPOSAL


def test_schema_keys_and_caps_equal_the_validators():
    from quant.triage import types as tt
    s = _schema()
    assert set(s["required"]) == set(tt._TOP_REQUIRED)
    assert set(s["properties"]) == set(tt._TOP_REQUIRED) | set(tt._TOP_OPTIONAL)
    assert s["additionalProperties"] is False
    p = s["properties"]
    assert p["claims"]["maxItems"] == T.MAX_CLAIMS and p["citations"]["maxItems"] == T.MAX_CITATIONS
    assert p["classification"]["properties"]["alternatives"]["maxItems"] == T.MAX_ALTERNATIVES
    assert p["negative_checks"]["maxItems"] == T.MAX_NEGATIVE_CHECKS
    assert p["missing_evidence"]["maxItems"] == p["needs_human"]["maxItems"] == T.MAX_LIST_ITEMS
    assert p["resolution"]["properties"]["factor_inputs"]["maxItems"] == T.MAX_FACTOR_INPUTS
    assert p["claims"]["items"]["properties"]["text"]["maxLength"] == T.MAX_CLAIM_CHARS
    assert p["missing_evidence"]["items"]["maxLength"] == T.MAX_TEXT_CHARS
    alt = p["classification"]["properties"]["alternatives"]["items"]["properties"]["why_not"]
    assert alt["maxLength"] == T.MAX_TEXT_CHARS
    assert s["x-caps"] == {"payload_bytes": T.MAX_PAYLOAD_BYTES, "quote_words": T.MAX_QUOTE_WORDS,
                           "pack_bytes": T.MAX_PACK_BYTES, "evidence_json_bytes": T.MAX_EVIDENCE_JSON_BYTES}


def _check(schema, node, value, path="$"):
    """A small checker for the subset of JSON Schema the mirror uses. Returns a list of problems."""
    node = _deref(schema, node)
    errs = []
    if "const" in node and value != node["const"]:
        errs.append(f"{path}: not const")
    if "enum" in node and value not in node["enum"]:
        errs.append(f"{path}: {value!r} not in enum")
    types = node.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        ok = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
              "number": isinstance(value, (int, float)) and not isinstance(value, bool),
              "integer": isinstance(value, int) and not isinstance(value, bool),
              "boolean": isinstance(value, bool), "null": value is None}
        if not any(ok[t] for t in types):
            return [f"{path}: type {type(value).__name__} not in {types}"]
    if isinstance(value, str):
        if "maxLength" in node and len(value) > node["maxLength"]:
            errs.append(f"{path}: too long")
        if "minLength" in node and len(value) < node["minLength"]:
            errs.append(f"{path}: too short")
        if "pattern" in node and not re.search(node["pattern"], value):
            errs.append(f"{path}: pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            errs.append(f"{path}: below minimum")
        if "exclusiveMinimum" in node and value <= node["exclusiveMinimum"]:
            errs.append(f"{path}: not above exclusiveMinimum")
    if isinstance(value, list):
        if "maxItems" in node and len(value) > node["maxItems"]:
            errs.append(f"{path}: too many items")
        for i, v in enumerate(value):
            if "items" in node:
                errs += _check(schema, node["items"], v, f"{path}[{i}]")
    if isinstance(value, dict):
        props = node.get("properties", {})
        for k in node.get("required", []):
            if k not in value:
                errs.append(f"{path}: missing {k}")
        if node.get("additionalProperties") is False:
            errs += [f"{path}: unknown key {k}" for k in value if k not in props]
        for k, v in value.items():
            if k in props:
                errs += _check(schema, props[k], v, f"{path}.{k}")
    return errs


def _recorded_proposals():
    return [json.loads(p.read_text())["proposal"] for p in sorted(te.RUNS_DIR.rglob("*.json"))]


def test_schema_accepts_the_recorded_proposals_the_validator_accepts_and_rejects_mutations(replayed):
    s = _schema()
    props = _recorded_proposals()
    assert len(props) == 4
    for prop in props:
        assert _check(s, s, prop) == [], prop["key"]
        assert T.check_schema(prop, producer_kind="llm") == []
    bad = copy.deepcopy(props[0])
    bad["classification"]["label"] = "rug_pull"
    assert _check(s, s, bad)
    bad = copy.deepcopy(props[0])
    bad["surprise"] = 1
    assert _check(s, s, bad)
    bad = copy.deepcopy(props[0])
    bad["citations"][0]["capture_id"] = "TD-xyz"
    assert _check(s, s, bad)


def test_contract_documents_every_validator_rule_and_the_command_boundary():
    text = CONTRACT.read_text()
    for n in range(1, 11):
        assert f"V{n}" in text, f"rule V{n}"
    for must in ("triage submit", "triage attach", "triage list", "triage show", "--actor-kind llm", "--by llm:",
                 "--url", "published_at", "captured_at", "cutoff", "untrusted", "self_confidence"):
        assert must in text, must
    forbidden = ("data actions-resolve", "prices action-add", "prices action-clear", "kb approve", "triage reject",
                 "triage collect")
    sect = text.split("## Forbidden", 1)[1].split("\n## ", 1)[0]
    for f in forbidden:
        assert f in sect, f


def test_wrapper_agent_follows_the_contract_and_states_the_label_rule():
    text = WRAPPER.read_text()
    head, body = text.split("---", 2)[1:3]
    fm = dict(line.split(":", 1) for line in head.strip().splitlines() if ":" in line)
    fm = {k.strip(): v.strip() for k, v in fm.items()}
    assert fm["name"] == "evidence-triage" and fm["model"] == "sonnet" and fm["description"]
    assert [t.strip() for t in fm["tools"].split(",")] == ["Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"]
    assert "docs/operations/triage_agent_contract.md" in body and "docs/operations/triage_agent_prompt.md" in body
    assert "--actor-kind llm --by llm:" in body and "label" in body
    for f in ("data actions-resolve", "prices action-add", "prices action-clear", "kb approve", "triage reject", "triage collect"):
        assert f in body, f
    for c in ("triage list", "triage show", "triage attach", "triage submit"):
        assert c in body, c


def test_prompt_names_every_label_and_the_output_rules():
    text = PROMPT.read_text()
    for label in T.LABELS:
        assert label in text, label
    for must in ("triage.proposal/1", "unknown", "missing_evidence", "producer.name", "prompt_sha256", "untrusted"):
        assert must in text, must


def test_agent_documents_never_contain_a_secret_or_a_private_path():
    for path in (CONTRACT, PROMPT, SCHEMA_FILE, WRAPPER):
        text = path.read_text()
        assert "/Users/" not in text and "sk-" not in text and ".env" not in text, path.name


# ------------------------------------------------------------------ the script itself stays offline and LLM-free

def test_the_script_imports_no_network_client_no_llm_and_no_factor_code():
    tree = ast.parse(SCRIPT.read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
    banned = ("anthropic", "openai", "requests", "urllib", "http", "socket", "yfinance", "httpx", "quant.factors",
              "quant.model", "quant.data.yahoo")
    assert not [m for m in mods if any(m == b or m.startswith(b + ".") for b in banned)], mods
    text = SCRIPT.read_text()
    assert "commit(" not in text and "with conn" not in text and "ledger_events" not in text
