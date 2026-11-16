#!/usr/bin/env python3
"""Evaluation harness for evidence triage (design section 9).

Commands (all offline; nothing here calls a model, the network, Jira or a real database):

  run         --set dev --tier t0 --day day0|day5 | --tier t1 [--json]
              Replays the golden set through the rules and scores it against the labels.
              T0 is price-only (recorded features). T1 adds hand-written document fixtures whose text
              is SYNTHETIC and not point-in-time true, so T1 is a regression check and never a claim
              about live accuracy. Only the dev split may be run here.
  holdout     --once [--json]
              The one sanctioned look at the holdout split. It writes holdout_reported.json beside
              MANIFEST.json and records the report hash in the manifest (holdout_report_sha256), and it
              refuses to run again when either exists. The marker path is fixed: there is no option to move
              it. Commit both files right after the look, or a fresh checkout can look again.
  score-llm   --runs DIR [--rebuild-examples] [--allow-pack-mismatch] [--fail-on-stale-prompt] [--json]
              Replays recorded agent runs (DIR/<model>/<prompt_sha256>/<case_id>.json) through the real
              validator (quant.triage.validate) against a disposable state database built from the
              fixture world. Each run is bound to the pack it embeds (what 'triage show --json' printed),
              and that pack's features and evidence must equal the replay world's: V5, V6 and V7 read them,
              so a run with edited features would pass while proving nothing. A mismatch is a problem (exit 2)
              and stays out of the pass rate unless --allow-pack-mismatch says the run cannot be rebuilt.
              A run without an embedded pack is checked against the replay world's pack. A run recorded under
              an older prompt is shown as a warning; --fail-on-stale-prompt makes it a problem. No model call.
              --rebuild-examples first rewrites the hashes of the hand-written examples.
  live-report --db PATH|file:...?mode=ro [--json]
              Reads proposals and human decisions from a copy of a state database, opened immutable so
              that no -wal or -shm file appears beside it. Point it at a copy, not a running database.
              Every metric is split per producer (engine, llm) and nothing is pooled: outcomes (accepted,
              modified, rejected, no_call, pending, overtaken), accept and override rates, dangerous errors
              against the human's final resolution, and the llm's added value on engine-abstained cases.
              no_call (a human resolved through a proposal that made no resolve call) is a third outcome and
              stays out of the accept and override rates.
  manifest    --check | --write [--label-rev N] [--golden-version N]
              Verifies or regenerates tests/fixtures/triage/golden/MANIFEST.json (append-only).

Exit codes: 0 ok, 1 usage or other error, 2 gate failed (a dangerous error; for score-llm also nothing
replayed, an unreadable run file, a run naming no golden case or a run whose pack differs from the replay
world), 3 refused (tampered golden set, holdout
already reported, unsafe or missing database, missing runs directory).

Rules for reading the numbers. A rate whose denominator is zero is ``null`` with no interval: unknown,
not 0%. An abstention (label ``unknown``) is never a correct or a wrong call; it is shown in coverage
and in accuracy-with-abstain-counted-wrong. Thresholds in quant/triage/rules.py were chosen after seeing
all 36 golden rows, so every T0 and T1 number is an upper bound and not a forecast. Live accuracy
comes from ``live-report`` and is advisory until n is about 30.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import re
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quant.errors import Refused  # noqa: E402
from quant.triage import (  # noqa: E402
    evidence, features as feat, pack as pack_mod, pit, rules, store as triage_store, types as T, validate,
)

GOLDEN_DIR = ROOT / "tests" / "fixtures" / "triage" / "golden"
RUNS_DIR = ROOT / "tests" / "fixtures" / "triage" / "llm_runs"
PROMPT_PATH = ROOT / "docs" / "operations" / "triage_agent_prompt.md"
HOLDOUT_MARKER_NAME = "holdout_reported.json"
HOLDOUT_MARKER = GOLDEN_DIR / HOLDOUT_MARKER_NAME

MANIFEST_SCHEMA = "triage.golden_manifest/1"
RUN_SCHEMA = "triage.llm_run/1"
REPORT_SCHEMA = "triage.eval_report/1"
HOLDOUT_SCHEMA = "triage.holdout_report/1"

#: dev / holdout split, frozen before any tuning: holdout when sha256(case_id)[:8] mod 10 is below 3.
SPLIT_MOD = 10
SPLIT_HOLDOUT_BELOW = 3
#: Fields of the source cases that neither inputs.json nor labels.json keeps (manifest source.fields_dropped).
DROPPED_FIELDS = ("security_id", "nse_sector", "sector_group_is_current_not_pit")
DROPPED_NOTE = ("security_id and nse_sector are state-database identifiers the classifier does not read; "
                "sector_group_is_current_not_pit was true for all 36 cases: sector peers are the current grouping, "
                "not point-in-time.")
#: The five known cases from the operator brief are always holdout.
GROUND_TRUTH = ("TMPV_2025-10-14", "VEDL_2026-04-30", "TRENT_2026-01-01", "HEG_2026-09-07", "POLICYBZR_2026-09-24")

SET_NAMES = ("dev", "holdout", "all")
TIERS = ("t0", "t1")
DAYS = ("day0", "day5")
CONFIDENCES = T.CONFIDENCES
FACTOR_TOLERANCE_PCT = 2.0                # design section 9: factor within 2% of the exchange-derived one
ACCURACY_TARGET = 0.90                    # design section 9: with T1 documents, abstain counted wrong
ACCURACY_MIN_N = 30                       # below this the target is undecidable: 1 of 1 is 100% and proves nothing
LIVE_CLAIM_N = 30                         # design section 9: n = 30 live cases before a live claim
_NON_TRANSFER_TRUTH = ("genuine_event", "pending_listing", "vendor_error_other")
_DOC_META = ("synthetic", "body_text", "reference_url", "note")


class GoldenError(Exception):
    """The golden set is missing, altered, or an update would break append-only."""


class RefusedDb(Exception):
    """A database argument that is not a safe read-only target."""


# ------------------------------------------------------------------ small helpers

def sha256_file(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj)).hexdigest()


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """Wilson score interval for k successes in n trials. None when n is 0: no sample is unknown, not [0, 0]."""
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    if n == 0:
        return None
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate_block(k: int, n: int) -> dict[str, Any]:
    """``{k, n, rate, wilson95}``; rate and interval are None when n is 0."""
    ci = wilson(k, n)
    return {"k": k, "n": n, "rate": None if n == 0 else k / n,
            "wilson95": None if ci is None else [round(ci[0], 4), round(ci[1], 4)]}


def _applies_factor(decision: Mapping[str, Any]) -> bool:
    """A resolve proposal whose factor is a number other than 1.0 changes a security's TRI."""
    f = decision.get("factor")
    return decision.get("action") == "resolve" and isinstance(f, (int, float)) and not isinstance(f, bool) \
        and abs(f - 1.0) > 1e-9


def is_dangerous(decision: Mapping[str, Any], true_label: str) -> str | None:
    """Design sections 9 and 11. ``decision`` has ``label``, ``confidence``, ``action`` and ``factor``.

    Dangerous: a value-transfer label at medium or high on a truly genuine, pending or vendor case; a factor
    applied (resolve, factor other than 1.0) at medium or high under ANY label on such a case, which is the
    'spurious adjustment' (a vendor-ratio factor under ``vendor_error_other`` moves TRI as much as a
    demerger does); or ``genuine_event`` at any confidence on a true demerger, scheme or rights case.
    """
    label, conf = decision.get("label"), decision.get("confidence")
    if label in T.VALUE_TRANSFER_LABELS and conf in ("medium", "high") and true_label in _NON_TRANSFER_TRUTH:
        return "value_transfer_on_non_transfer"
    if conf in ("medium", "high") and true_label in _NON_TRANSFER_TRUTH and _applies_factor(decision):
        return "spurious_adjustment"
    if label == "genuine_event" and true_label in T.VALUE_TRANSFER_LABELS:
        return "genuine_on_value_transfer"
    return None


# ------------------------------------------------------------------ split and golden set

def split_bucket(case_id: str) -> int:
    return int(hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:8], 16) % SPLIT_MOD


def assign_split(case_id: str) -> str:
    return "holdout" if case_id in GROUND_TRUTH or split_bucket(case_id) < SPLIT_HOLDOUT_BELOW else "dev"


@dataclass
class Golden:
    root: Path
    inputs: list[dict[str, Any]]
    labels: dict[str, dict[str, Any]]
    documents: dict[str, Any]
    manifest: dict[str, Any]
    manifest_sha256: str

    @property
    def by_id(self) -> dict[str, dict[str, Any]]:
        return {c["case_id"]: c for c in self.inputs}

    def ids(self, set_name: str) -> list[str]:
        """Case ids of a split, in golden order."""
        if set_name not in SET_NAMES:
            raise ValueError(f"unknown set {set_name!r}")
        if set_name == "all":
            return [c["case_id"] for c in self.inputs]
        wanted = set(self.manifest["split"][set_name])
        return [c["case_id"] for c in self.inputs if c["case_id"] in wanted]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoldenError(f"{path.name}: cannot read ({exc})") from exc


def load_golden(root: Path | str = GOLDEN_DIR, *, verify: bool = True) -> Golden:
    """Load the golden set. ``verify`` refuses a file whose sha256 differs from MANIFEST.json."""
    root = Path(root)
    manifest_path = root / "MANIFEST.json"
    manifest = _read_json(manifest_path)
    if verify:
        for name, sha in manifest.get("files", {}).items():
            if not (root / name).is_file() or sha256_file(root / name) != sha:
                raise GoldenError(f"{name}: sha256 differs from MANIFEST.json (golden_version "
                                  f"{manifest.get('golden_version')}); the golden set is append-only")
    inputs, labels, documents = _read_json(root / "inputs.json"), _read_json(root / "labels.json"), \
        _read_json(root / "t1_documents.json")
    ids = [c["case_id"] for c in inputs]
    if len(set(ids)) != len(ids) or ids != list(labels):
        raise GoldenError("inputs.json and labels.json do not list the same case ids in the same order")
    if verify and manifest.get("n_cases") != len(ids):
        raise GoldenError(f"MANIFEST.json says {manifest.get('n_cases')} cases, inputs.json has {len(ids)}")
    return Golden(root, inputs, labels, documents, manifest, sha256_file(manifest_path))


def build_manifest(root: Path | str, *, previous: Mapping[str, Any] | None = None,
                   golden_version: int | None = None, label_rev: int | None = None) -> dict[str, Any]:
    """Compute MANIFEST.json for the files under ``root``. Append-only against ``previous``.

    Cases never leave, their inputs never change, and a changed label needs a higher ``label_rev``.
    An existing case keeps its dev or holdout assignment; a new case gets ``assign_split``. Adding
    cases raises ``golden_version`` by one unless a version is given.
    """
    root = Path(root)
    inputs, labels, documents = _read_json(root / "inputs.json"), _read_json(root / "labels.json"), \
        _read_json(root / "t1_documents.json")
    ids = [c["case_id"] for c in inputs]
    if len(set(ids)) != len(ids) or ids != list(labels):
        raise GoldenError("inputs.json and labels.json do not list the same case ids in the same order")
    stray = sorted(set(documents.get("cases", {})) - set(ids))
    if stray:
        raise GoldenError(f"t1_documents.json names cases that are not in the golden set: {stray}")
    input_sha = {c["case_id"]: _sha(c) for c in inputs}
    label_sha = {cid: _sha(v) for cid, v in labels.items()}

    prev_split: dict[str, str] = {}
    version, rev = 1, 1
    source: Any = {"description": "Copied from the eval cases (36 labelled anomalies) and split into inputs and labels.",
                   "copied_on": "2026-10-01", "cases_json_sha256": None,
                   "fields_dropped": list(DROPPED_FIELDS), "fields_dropped_note": DROPPED_NOTE}
    if previous:
        gone = sorted(set(previous["case_input_sha256"]) - set(ids))
        if gone:
            raise GoldenError(f"case(s) removed from the golden set: {gone}; cases are append-only")
        edited = sorted(cid for cid, sha in previous["case_input_sha256"].items() if input_sha[cid] != sha)
        if edited:
            raise GoldenError(f"inputs of an existing case changed: {edited}; cases are append-only")
        for name in ("dev", "holdout"):
            prev_split.update({cid: name for cid in previous["split"][name]})
        version, rev = previous["golden_version"], previous["label_rev"]
        source = previous.get("source", source)
        relabelled = sorted(cid for cid, sha in previous["case_label_sha256"].items() if label_sha[cid] != sha)
        added = [cid for cid in ids if cid not in previous["case_input_sha256"]]
        if relabelled and (label_rev is None or label_rev <= rev):
            raise GoldenError(f"label(s) changed for {relabelled}: label_rev must increase above {rev} (pass --label-rev)")
        if added and golden_version is None:
            golden_version = version + 1
    if golden_version is not None:
        if previous and golden_version < version:
            raise GoldenError("golden_version cannot go down")
        version = golden_version
    if label_rev is not None:
        if previous and label_rev < rev:
            raise GoldenError("label_rev cannot go down")
        rev = label_rev

    assigned = {cid: prev_split.get(cid) or assign_split(cid) for cid in ids}
    wrong = [cid for cid in GROUND_TRUTH if cid in assigned and assigned[cid] != "holdout"]
    if wrong:
        raise GoldenError(f"ground-truth case(s) must be holdout: {wrong}")
    return {
        "schema": MANIFEST_SCHEMA,
        "golden_version": version,
        "label_rev": rev,
        "n_cases": len(ids),
        "files": {name: sha256_file(root / name) for name in ("inputs.json", "labels.json", "t1_documents.json")},
        "case_input_sha256": input_sha,
        "case_label_sha256": label_sha,
        "split": {
            "rule": f"holdout when int(sha256(case_id)[:8], 16) % {SPLIT_MOD} < {SPLIT_HOLDOUT_BELOW}, or the case is ground truth",
            "frozen": True,
            "ground_truth_cases": list(GROUND_TRUTH),
            "dev": [cid for cid in ids if assigned[cid] == "dev"],
            "holdout": [cid for cid in ids if assigned[cid] == "holdout"],
        },
        "holdout_report_sha256": (previous or {}).get("holdout_report_sha256"),
        "source": source,
        "notes": ["Cases are append-only. Thresholds were tuned with all 36 rows visible, so the split limits "
                  "future tuning, not past tuning.",
                  "T1 document fixtures are synthetic and not point-in-time true.",
                  "T1 figures (for example prev_close and spos_price) were copied from the labels, so T1 accuracy is "
                  "partly circular: a regression check, never a measure of live accuracy."],
    }


def write_manifest(root: Path | str = GOLDEN_DIR, *, golden_version: int | None = None,
                   label_rev: int | None = None) -> dict[str, Any]:
    root = Path(root)
    path = root / "MANIFEST.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    manifest = build_manifest(root, previous=previous, golden_version=golden_version, label_rev=label_rev)
    text = _dump(manifest)
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return manifest


# ------------------------------------------------------------------ predictions (inputs only; labels are not read here)

def _recorded_features(case: Mapping[str, Any], cases: Sequence[Mapping[str, Any]], *, post_bars: bool,
                       series: str | None = None, placeholder: bool | None = None) -> feat.Features:
    ph = case.get("placeholder") is True if placeholder is None else placeholder
    return feat.from_recorded(case, cases, post_bars_available=post_bars, placeholder=ph, series=series)


def _t1_features(case: Mapping[str, Any], cases: Sequence[Mapping[str, Any]], spec: Mapping[str, Any], *,
                 post_bars: bool) -> feat.Features:
    over = dict(spec.get("features_override") or {})
    f = _recorded_features(case, cases, post_bars=post_bars, series=over.pop("series", None),
                           placeholder=over.pop("placeholder", None))
    return dataclasses.replace(f, **over) if over else f


def _build_items(case: Mapping[str, Any], spec: Mapping[str, Any], docs: Mapping[str, Any], *,
                 archive_dir: Path | str) -> tuple[list[evidence.EvidenceItem], list[dict[str, Any]], str]:
    """Envelopes for one T1 case without a database: (items, register-ready fields, captured_at)."""
    as_of = case["ex_date"]
    captured_at = f"{as_of}T{docs['captured_time_utc']}"
    cutoff = pit.cutoff_utc(as_of)
    items, fields_list = [], []
    for d in spec["documents"]:
        fields = {k: v for k, v in d.items() if k not in _DOC_META}
        fields["subject"] = {"isin": case["isin"], "ex_date": case["ex_date"]}
        if d.get("body_text") is not None:
            fields["body_sha256"] = evidence.store_body(archive_dir, d["body_text"].encode("utf-8"))
        env = evidence.normalise_envelope(fields)
        sha = T.sha256_hex(T.canonical_bytes(env))
        row = {"kind": T.CAPTURE_KIND, "captured_at": captured_at, "sha256": sha}
        ok, reason = pit.admissible(env, row, cutoff)
        if not ok:
            raise GoldenError(f"{case['case_id']}: a document fixture is not admissible at its own cutoff ({reason})")
        decisive, _ = pit.admissible(env, row, cutoff, decisive=True)
        items.append(evidence.EvidenceItem(T.CAPTURE_ID_PREFIX + sha[:16], sha, captured_at, env, decisive, reason))
        fields_list.append(fields)
    return items, fields_list, captured_at


def _decision_row(case_id: str, d: rules.Decision) -> dict[str, Any]:
    return {"case_id": case_id, "predicted": d.label, "confidence": d.confidence, "rule_id": d.rule_id,
            "action": d.action, "resolve_kind": d.resolve_kind, "factor": d.factor,
            "abstained": d.label == "unknown", "recheck_on": d.recheck_on}


def predict(golden: Golden, *, tier: str, day: str, case_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Classify cases from the recorded inputs (and T1 documents). Labels are never read here.

    T1 returns only the cases that have document fixtures, each with the price-only label for contrast.
    """
    if tier not in TIERS or day not in DAYS:
        raise ValueError(f"tier must be one of {TIERS} and day one of {DAYS}")
    post_bars = day == "day5"
    cases = golden.inputs
    by_id = golden.by_id
    out: list[dict[str, Any]] = []
    if tier == "t0":
        for cid in case_ids:
            d = rules.classify(_recorded_features(by_id[cid], cases, post_bars=post_bars))
            out.append(_decision_row(cid, d))
        return out
    with tempfile.TemporaryDirectory(prefix="triage_eval_") as tmp:
        for cid in case_ids:
            spec = golden.documents["cases"].get(cid)
            if spec is None:
                continue
            case = by_id[cid]
            items, _, _ = _build_items(case, spec, golden.documents, archive_dir=tmp)
            f = _t1_features(case, cases, spec, post_bars=post_bars)
            row = _decision_row(cid, rules.classify(f, items, archive_dir=tmp))
            row["price_only_label"] = rules.classify(_recorded_features(case, cases, post_bars=post_bars)).label
            row["synthetic_documents"] = sum(1 for d in spec["documents"] if d.get("synthetic") is True)
            out.append(row)
    return out


# ------------------------------------------------------------------ scoring

def _attach_truth(rows: Sequence[dict[str, Any]], labels: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        lab = labels[r["case_id"]]
        r = dict(r)
        truth = lab["label_class"]
        r["true_label"] = truth
        r["label_confidence"] = lab["label_confidence"]
        r["correct"] = None if r["abstained"] else r["predicted"] == truth
        r["dangerous"] = None if r["abstained"] else is_dangerous(
            {"label": r["predicted"], "confidence": r["confidence"], "action": r["action"], "factor": r["factor"]}, truth)
        lf = lab.get("label_factor")
        r["label_factor"] = lf if isinstance(lf, (int, float)) else None
        r["factor_error_pct"] = None
        if r["factor"] is not None and r["label_factor"]:
            r["factor_error_pct"] = round((r["factor"] - r["label_factor"]) / r["label_factor"] * 100.0, 4)
        out.append(r)
    return out


def score_scope(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Metrics for scored rows (``_attach_truth`` output)."""
    n = len(rows)
    fired = [r for r in rows if not r["abstained"]]
    correct = [r for r in fired if r["correct"]]
    by_conf = {c: [r for r in fired if r["confidence"] == c] for c in CONFIDENCES}
    by_rule: dict[str, dict[str, int]] = {}
    for r in fired:
        b = by_rule.setdefault(r["rule_id"] or "none", {"fired": 0, "correct": 0})
        b["fired"] += 1
        b["correct"] += 1 if r["correct"] else 0
    danger = [{"case_id": r["case_id"], "kind": r["dangerous"]} for r in rows if r["dangerous"]]
    return {
        "n_cases": n,
        "coverage": rate_block(len(fired), n),
        "precision_fired": rate_block(len(correct), len(fired)),
        "precision_by_confidence": {c: rate_block(sum(1 for r in rs if r["correct"]), len(rs)) for c, rs in by_conf.items()},
        "accuracy_abstain_wrong": rate_block(len(correct), n),
        "dangerous_errors": {"count": len(danger), "cases": danger},
        "by_rule": by_rule,
        "abstained_by_label": dict(Counter(r["true_label"] for r in rows if r["abstained"])),
    }


def _market_implied(golden: Golden, case_ids: Sequence[str]) -> dict[str, Any]:
    """Gap between 1/gross and the labelled exchange-derived factor, per value-transfer case, against the declared band."""
    by_id = golden.by_id
    band = T.MARKET_IMPLIED_UNCERTAINTY_PCT
    cases = []
    for cid in case_ids:
        lab = golden.labels[cid]
        lf = lab.get("label_factor")
        gross = by_id[cid].get("gross_factor")
        if lab["label_class"] in T.VALUE_TRANSFER_LABELS and isinstance(lf, (int, float)) and lf and gross:
            gap = (1.0 / gross - lf) / lf * 100.0
            cases.append({"case_id": cid, "market_implied": round(1.0 / gross, 4), "label_factor": lf,
                          "gap_pct": round(gap, 2), "covered": abs(gap) <= band})
    return {"declared_band_pct": band, "n": len(cases), "n_covered": sum(1 for c in cases if c["covered"]),
            "cases": cases}


def accuracy_target_block(k: int, n: int) -> dict[str, Any]:
    """The design's 90% accuracy target for T1 (abstain counted wrong), with the evidence for it.

    ``met`` is None while n is below ACCURACY_MIN_N: a point estimate on so few cases cannot meet or miss a target
    (1 of 1 is 100%, 6 of 7 is 86%, and neither says anything). ``point_estimate_meets_target`` still shows the
    raw comparison so nothing is hidden, and the Wilson interval sits beside it.
    """
    ci = wilson(k, n)
    rate = None if n == 0 else k / n
    decidable = n >= ACCURACY_MIN_N
    point = None if rate is None else rate >= ACCURACY_TARGET
    return {"target": ACCURACY_TARGET, "min_n": ACCURACY_MIN_N, "k": k, "n": n, "rate": rate,
            "wilson95": None if ci is None else [round(ci[0], 4), round(ci[1], 4)],
            "decidable": decidable, "point_estimate_meets_target": point, "met": point if decidable else None}


def _factor_check(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    errs = [abs(r["factor_error_pct"]) for r in rows
            if r["predicted"] in T.VALUE_TRANSFER_LABELS and r["factor_error_pct"] is not None]
    return {"tolerance_pct": FACTOR_TOLERANCE_PCT, "n": len(errs),
            "within_2pct": sum(1 for e in errs if e <= FACTOR_TOLERANCE_PCT),
            "max_abs_error_pct": None if not errs else round(max(errs), 4)}


def _caveats(tier: str, golden: Golden) -> list[str]:
    observed = {v["label_class"] for v in golden.labels.values()}
    unsampled = [c for c in T.LABELS if c != "unknown" and c not in observed]
    out = [
        "Thresholds were chosen after seeing all 36 rows, so these numbers are an upper bound, not a forecast.",
        f"No golden case exists for {unsampled}: those classes are excluded from every metric until sampled.",
        "Headline scope drops the cases whose own label has low confidence; the all-cases scope keeps them.",
        "Most labels rest on news; 3 were checked against a primary document. A label error moves a metric by about 3 points per case.",
    ]
    if tier == "t1":
        out.append("T1 documents are synthetic and not point-in-time true: a regression check, never a claim about live accuracy.")
    else:
        out.append("T0 is price-only on recorded features; peers are not point-in-time.")
    return out


def run_report(golden: Golden, *, set_name: str, tier: str, day: str = "day0") -> dict[str, Any]:
    ids = golden.ids(set_name)
    rows = _attach_truth(predict(golden, tier=tier, day=day, case_ids=ids), golden.labels)
    observed = {v["label_class"] for v in golden.labels.values()}
    headline_rows = [r for r in rows if r["label_confidence"] != "low"]
    everything = score_scope(rows)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA, "tier": tier, "day": day, "set": set_name,
        "golden_version": golden.manifest["golden_version"], "label_rev": golden.manifest["label_rev"],
        "manifest_sha256": golden.manifest_sha256, "rules_version": rules.RULES_VERSION,
        "caveats": _caveats(tier, golden),
        "unsampled_classes": [c for c in T.LABELS if c != "unknown" and c not in observed],
        "all": everything, "headline": score_scope(headline_rows),
        "gates": {"dangerous_errors": everything["dangerous_errors"]["count"],
                  "passed": everything["dangerous_errors"]["count"] == 0},
        "cases": rows,
    }
    if tier == "t0":
        report["market_implied"] = _market_implied(golden, ids)
    else:
        acc = everything["accuracy_abstain_wrong"]
        report.update(documents_synthetic=True, pit_true=False, factor_check=_factor_check(rows),
                      accuracy_target=accuracy_target_block(acc["k"], acc["n"]))
    return report


def format_report(rep: Mapping[str, Any]) -> str:
    def pct(b: Mapping[str, Any]) -> str:
        if b["rate"] is None:
            return "n/a (no sample)"
        lo, hi = b["wilson95"]
        return f"{b['k']}/{b['n']} = {b['rate']:.1%} (Wilson 95% {lo:.1%} to {hi:.1%})"

    lines = [f"triage eval: tier {rep['tier']} {rep['day']} set {rep['set']} "
             f"(golden v{rep['golden_version']}, label rev {rep['label_rev']}, rules {rep['rules_version']})"]
    for scope in ("all", "headline"):
        s = rep[scope]
        lines += [f"[{scope}] cases {s['n_cases']}",
                  f"  coverage (non-abstain)            {pct(s['coverage'])}",
                  f"  precision of non-abstain          {pct(s['precision_fired'])}"]
        for c in CONFIDENCES:
            lines.append(f"    at {c:<6}                        {pct(s['precision_by_confidence'][c])}")
        lines += [f"  accuracy, abstain counted wrong   {pct(s['accuracy_abstain_wrong'])}",
                  f"  dangerous errors                  {s['dangerous_errors']['count']}"]
    if rep["tier"] == "t1":
        lines.append("  T1 documents are SYNTHETIC and not point-in-time true: regression only, no live claim.")
        t = rep["accuracy_target"]
        if t["decidable"]:
            verdict = "met" if t["met"] else "NOT met"
        else:
            verdict = f"undecidable at n={t['n']} (needs n >= {t['min_n']}); point estimate " + (
                "n/a" if t["point_estimate_meets_target"] is None
                else ("meets it" if t["point_estimate_meets_target"] else "misses it"))
        lines.append(f"  accuracy target {t['target']:.0%}: {verdict}; {pct(rep['all']['accuracy_abstain_wrong'])}")
    lines.append("gate: " + ("passed" if rep["gates"]["passed"] else "FAILED (dangerous error)"))
    lines += ["caveat: " + c for c in rep["caveats"][:1]]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ holdout, reported once

def holdout_marker(golden: Golden) -> Path:
    """The marker sits beside MANIFEST.json. It cannot be moved, so a fresh path never buys a second look."""
    return golden.root / HOLDOUT_MARKER_NAME


def check_holdout_unreported(golden: Golden) -> None:
    """Refuse when the holdout was already looked at: the marker exists or the manifest names a report hash."""
    marker = holdout_marker(golden)
    named = golden.manifest.get("holdout_report_sha256")
    if named or marker.exists():
        if not named:
            try:
                named = json.loads(marker.read_text(encoding="utf-8")).get("report_sha256")
            except (OSError, ValueError, AttributeError):
                named = None
        raise GoldenError(f"the holdout was already reported (report {str(named)[:12]}, marker {marker.name}, "
                          "manifest holdout_report_sha256); a second look is an owner decision, not a flag")


def record_holdout_look(golden: Golden, report: Mapping[str, Any]) -> None:
    """Write the marker (exclusive create) and name the report hash in MANIFEST.json. Nothing else in the manifest moves."""
    record = dict(report, reported_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with open(holdout_marker(golden), "x", encoding="utf-8") as fh:
        fh.write(_dump(record))
    path = golden.root / "MANIFEST.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["holdout_report_sha256"] = report["report_sha256"]
    path.write_text(_dump(manifest), encoding="utf-8")


def holdout_report(golden: Golden) -> dict[str, Any]:
    check_holdout_unreported(golden)
    reports = {
        "t0_day0": run_report(golden, set_name="holdout", tier="t0", day="day0"),
        "t0_day5": run_report(golden, set_name="holdout", tier="t0", day="day5"),
        "t1_day0": run_report(golden, set_name="holdout", tier="t1", day="day0"),
    }
    return {"schema": HOLDOUT_SCHEMA, "set": "holdout", "golden_version": golden.manifest["golden_version"],
            "label_rev": golden.manifest["label_rev"], "manifest_sha256": golden.manifest_sha256,
            "report_sha256": _sha({"manifest": golden.manifest_sha256, "reports": reports}), "reports": reports}


# ------------------------------------------------------------------ score-llm: recorded runs through the real validator

@dataclass
class _World:
    as_of: str
    conn: Any
    archive_dir: Path
    pack: dict[str, Any]
    pack_sha256: str
    engine: dict[str, Any]
    capture_ids: list[str]


def _build_world(golden: Golden, case_id: str, tmp: Path) -> _World:
    """A disposable state database, archive and pack for one golden case, day 0, with its T1 documents."""
    from quant.config import load as load_config
    from quant.db.core import apply_schema, connect
    from quant.run import RunContext
    from quant.types import Actor, FrozenClock

    case = golden.by_id[case_id]
    spec = golden.documents["cases"].get(case_id) or {"documents": []}
    root = tmp / case_id
    db = root / "state.db"
    root.mkdir(parents=True)
    cfg = load_config().with_paths(db=db, prices_db=root / "prices.db", data_dir=root / "data",
                                   archive_dir=root / "archive", knowledge_dir=root / "knowledge", ui_dir=root / "ui")
    setup = connect(db)
    apply_schema(setup, kind="state")
    setup.close()
    items, fields_list, captured_at = _build_items(case, spec, golden.documents, archive_dir=cfg.paths.archive_dir)
    as_of = case["ex_date"]
    if fields_list:
        ctx = RunContext(as_of=as_of, kind="triage", track="live", cfg=cfg, clock=FrozenClock(captured_at),
                         actor=Actor(kind="system", name="triage_eval"))
        with ctx:
            ids = [evidence.register(ctx, f).capture_id for f in fields_list]
        if ids != [i.capture_id for i in items]:
            raise GoldenError(f"{case_id}: archived capture ids differ from the computed ones")
    f = _t1_features(case, golden.inputs, spec, post_bars=False) if spec["documents"] \
        else _recorded_features(case, golden.inputs, post_bars=False)
    decision = rules.classify(f, items, archive_dir=cfg.paths.archive_dir)
    pack = _world_pack(f, items, decision, case, as_of)
    sha, _ = triage_store.pack_bytes(pack)
    return _World(as_of, connect(db, readonly=True), Path(cfg.paths.archive_dir), pack, sha,
                  {"as_of": as_of, "classification": {"label": decision.label, "rule_id": decision.rule_id}},
                  [i.capture_id for i in items])


def _world_pack(f: feat.Features, items: Sequence[evidence.EvidenceItem], decision: rules.Decision,
                case: Mapping[str, Any], as_of: str) -> dict[str, Any]:
    """A pack with the keys ``quant.triage.pack.build_one`` writes, built from the recorded features.

    There is no price store in the replay world, so the price source is ``skipped`` (never ran), not
    ``empty`` (answered with nothing), and the vintage names no bars.
    """
    cutoff = pit.cutoff_utc(as_of)
    vintage = {"vintage_at": cutoff, "subject_bars_sha256": None, "n_bars": 0, "last_bar_date": None}
    status = pack_mod.source_status(f, items, [], vintage)
    status["price_store"] = "skipped"
    pack: dict[str, Any] = {
        "schema": T.SCHEMA_PACK, "rules_version": rules.RULES_VERSION,
        "subject": {"key": f"{case['isin']}@{case['ex_date']}", "isin": case["isin"], "ex_date": case["ex_date"],
                    "symbol": case.get("symbol"), "security_id": None, "anomaly_kind": f.anomaly_kind},
        "as_of": as_of, "cutoff_utc": cutoff, "price_store_vintage": vintage,
        "features": feat.flat(f, items, product_range=rules.R6_PRODUCT_RANGE),
        "source_status": status, "rejected_evidence": [],
    }
    pack_mod._fit(pack, pack_mod._order(items, decision.cited))
    return pack


def describe_world(golden: Golden, case_id: str) -> dict[str, Any]:
    """What a recorded run for ``case_id`` is validated against: as_of, pack, pack hash and capture ids."""
    with tempfile.TemporaryDirectory(prefix="triage_eval_") as tmp:
        w = _build_world(golden, case_id, Path(tmp))
        try:
            return {"as_of": w.as_of, "pack": w.pack, "pack_sha256": w.pack_sha256, "capture_ids": w.capture_ids,
                    "engine_label": w.engine["classification"]["label"]}
        finally:
            w.conn.close()


def _error_code(err: str) -> str:
    parts = err.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else err


def _pack_differences(pack: Mapping[str, Any], world_pack: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """What an embedded pack changes against the replay world's: (top-level parts, feature names).

    Only ``features`` and ``evidence`` are compared. V5, V6 and V7 read the features (market_implied, needs_force,
    f:<name> numbers, vendor_conflict_required) and V3/V4 read the evidence. A real pack also carries rejected
    evidence and an omitted count that the fixture world cannot know, so those are not differences.
    """
    parts: list[str] = []
    names: list[str] = []
    feats, world_feats = pack.get("features"), world_pack["features"]
    if feats != world_feats:
        parts.append("features")
        if isinstance(feats, dict) and isinstance(world_feats, dict):
            names = sorted(k for k in set(feats) | set(world_feats) if feats.get(k) != world_feats.get(k))
    if pack.get("evidence") != world_pack.get("evidence"):
        parts.append("evidence")
    return parts, names


def _replay_one(golden: Golden, path: Path, rel: Path, worlds: dict[str, _World], tmp: Path,
                prompt_now: str | None, *, allow_pack_mismatch: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {"case_id": path.stem, "path": rel.as_posix(), "status": "unreadable"}
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return row
    if not isinstance(run, dict) or run.get("schema") != RUN_SCHEMA:
        return row
    row["case_id"] = run.get("case_id") if isinstance(run.get("case_id"), str) else path.stem
    if row["case_id"] not in golden.labels:
        row["status"] = "unknown_case"
        return row
    proposal = run.get("proposal")
    if not isinstance(proposal, dict):
        return row
    case_id = row["case_id"]
    if case_id not in worlds:
        worlds[case_id] = _build_world(golden, case_id, tmp)
    w = worlds[case_id]
    # The agent copies producer.pack_sha256 from the pack it was shown, so a run is bound to that pack. That proves
    # the run agrees with itself, not that the pack is the world's: the validator takes its features from the pack, so
    # an edited, re-hashed pack would pass. The pack is therefore also compared with the replay world's (below). Only
    # a run that did not record its pack falls back to the replay world's pack (and says so).
    embedded = run.get("pack")
    if isinstance(embedded, dict):
        try:
            pack, pack_sha = embedded, triage_store.pack_bytes(embedded)[0]
        except Refused:
            return row
        source = "embedded"
    else:
        pack, pack_sha, source = w.pack, w.pack_sha256, "replay_world"
    mismatch, mismatch_features = _pack_differences(pack, w.pack)
    res = validate.validate(proposal, conn=w.conn, as_of=w.as_of, pack=pack, engine_proposal=w.engine,
                            pack_sha256=pack_sha, archive_dir=w.archive_dir)
    cl = (res.proposal if res.ok else proposal).get("classification") or {}
    label, conf = cl.get("label"), cl.get("confidence")
    truth = golden.labels[case_id]["label_class"]
    prompt = run.get("prompt_sha256")
    resolution = (res.proposal if res.ok else proposal).get("resolution")
    resolution = resolution if isinstance(resolution, dict) else {}
    row.update(
        status="pack_mismatch" if mismatch and not allow_pack_mismatch else "replayed", model=run.get("model"),
        prompt_sha256=prompt, synthetic=run.get("synthetic") is True, pack_source=source,
        pack_matches_replay_world=not mismatch, pack_mismatch=mismatch, pack_mismatch_features=mismatch_features,
        pack_mismatch_allowed=bool(mismatch and allow_pack_mismatch),
        prompt_matches_current=None if prompt_now is None else prompt == prompt_now,
        validator_ok=res.ok, errors=list(res.errors), flags=list(res.flags), notes=list(res.notes),
        label=label, confidence=conf, engine_label=w.engine["classification"]["label"], true_label=truth,
        correct=label == truth if label != "unknown" else None,
        action=resolution.get("action"), factor=resolution.get("factor"),
        dangerous=is_dangerous({"label": label, "confidence": conf, "action": resolution.get("action"),
                                "factor": resolution.get("factor")}, truth)
        if isinstance(label, str) and isinstance(conf, str) else None)
    return row


def replay_runs(runs_dir: Path | str, golden: Golden, *, allow_pack_mismatch: bool = False,
                fail_on_stale_prompt: bool = False) -> dict[str, Any]:
    """Re-validate every recorded run under ``runs_dir`` and score it. No model call, no network.

    A run whose embedded pack differs from the replay world's (features or evidence) is a problem and is left out of
    every metric, unless ``allow_pack_mismatch``. A run recorded under another prompt is a warning, or a problem with
    ``fail_on_stale_prompt``.
    """
    runs_dir = Path(runs_dir)
    prompt_now = sha256_file(PROMPT_PATH) if PROMPT_PATH.is_file() else None
    rows: list[dict[str, Any]] = []
    worlds: dict[str, _World] = {}
    with tempfile.TemporaryDirectory(prefix="triage_eval_") as tmp:
        try:
            for path in sorted(runs_dir.rglob("*.json")) if runs_dir.is_dir() else []:
                rows.append(_replay_one(golden, path, path.relative_to(runs_dir), worlds, Path(tmp), prompt_now,
                                        allow_pack_mismatch=allow_pack_mismatch))
        finally:
            for w in worlds.values():
                w.conn.close()
    replayed = [r for r in rows if r["status"] == "replayed"]
    passed = [r for r in replayed if r["validator_ok"]]
    decided = [r for r in passed if r["label"] != "unknown"]
    codes: Counter = Counter()
    for r in replayed:
        codes.update({_error_code(e) for e in r["errors"]})
    blocked = [{"case_id": r["case_id"], "kind": r["dangerous"]} for r in replayed if not r["validator_ok"] and r["dangerous"]]
    danger = [{"case_id": r["case_id"], "kind": r["dangerous"]} for r in passed if r["dangerous"]]
    abstained_by_engine = [r for r in passed if r["engine_label"] == "unknown"]
    metrics = {
        "validator_pass": rate_block(len(passed), len(replayed)),
        "accuracy": rate_block(sum(1 for r in decided if r["correct"]), len(decided)),
        "agreement_with_engine": rate_block(sum(1 for r in passed if r["label"] == r["engine_label"]), len(passed)),
        "added_value_on_engine_abstentions": rate_block(
            sum(1 for r in abstained_by_engine if r["correct"]), len([r for r in abstained_by_engine if r["label"] != "unknown"])),
        "dangerous_errors": {"count": len(danger), "cases": danger},
        "blocked_dangerous_attempts": {"count": len(blocked), "cases": blocked},
        "error_codes": dict(codes),
    }
    stale = sum(1 for r in rows if r.get("prompt_matches_current") is False)
    problems = {"unreadable": sum(1 for r in rows if r["status"] == "unreadable"),
                "unknown_case": sum(1 for r in rows if r["status"] == "unknown_case"),
                "pack_mismatch": sum(1 for r in rows if r["status"] == "pack_mismatch"),
                "stale_prompt": stale if fail_on_stale_prompt else 0}
    warnings = {"stale_prompt": stale, "pack_mismatch_allowed": sum(1 for r in replayed if r["pack_mismatch_allowed"])}
    return {"schema": REPORT_SCHEMA, "tier": "score_llm", "runs_dir": runs_dir.name, "model_calls": 0,
            "synthetic_runs": sum(1 for r in replayed if r["synthetic"]), "runs": rows, "metrics": metrics,
            "problems": problems, "warnings": warnings,
            "caveats": ["Recorded runs under tests/fixtures are hand-written examples, not model output, unless a run says otherwise.",
                        "Unsupported-claim audit, closed-book and stripped-bundle arms, and run agreement need real recorded runs "
                        "and are not computed here.",
                        "Only engine-abstained cases measure added value; the fixture world gives the engine its documents, so few abstain.",
                        "Citations are checked against the replay world's captures (the golden case's T1 documents) and the engine "
                        "proposal is the replay world's; a run that cites captures from a real database cannot replay here yet."]}


EXAMPLE_MODEL = "hand_written_example"


def rebuild_examples(runs_dir: Path | str, golden: Golden) -> dict[str, Any]:
    """Rewrite the prompt and pack hashes of the hand-written example runs under ``runs_dir``.

    The examples are bound to the prompt file's bytes and to the pack the replay world builds, so an edit to
    ``triage_agent_prompt.md``, ``features.flat`` or the pack keys needs this. Only runs with
    ``model == "hand_written_example"`` and ``synthetic: true`` are touched; a recorded model run is never
    rewritten. A rewritten run is filed under the new prompt hash and its old file is removed. Nothing
    but hashes, the embedded pack and the ``illustrative`` marker on a transcript line changes.
    """
    runs_dir = Path(runs_dir)
    prompt = sha256_file(PROMPT_PATH)
    rewritten: list[str] = []
    for path in sorted(runs_dir.rglob("*.json")):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(run, dict) or run.get("schema") != RUN_SCHEMA or run.get("model") != EXAMPLE_MODEL \
                or run.get("synthetic") is not True or run.get("case_id") not in golden.labels \
                or not isinstance(run.get("proposal"), dict):
            continue
        world = describe_world(golden, run["case_id"])
        run["prompt_sha256"] = prompt
        run["pack"] = world["pack"]
        run["proposal"].setdefault("producer", {}).update(prompt_sha256=prompt, pack_sha256=world["pack_sha256"])
        for step in run.get("transcript", []):
            if isinstance(step, dict) and "stderr_first_line" in step:
                step["illustrative"] = True
        target = runs_dir / EXAMPLE_MODEL / prompt / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_dump(run), encoding="utf-8")
        if target.resolve() != path.resolve():
            path.unlink()
            if not any(path.parent.iterdir()):
                path.parent.rmdir()
        rewritten.append(run["case_id"])
    return {"schema": REPORT_SCHEMA, "tier": "score_llm_rebuild", "prompt_sha256": prompt, "rewritten": rewritten}


# ------------------------------------------------------------------ live-report: a read-only database

def _quote_path(text: str) -> str:
    return text.replace("%", "%25").replace("?", "%3F").replace("#", "%23")


def _unquote_path(text: str) -> str:
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), text)


def open_readonly(db: Path | str) -> sqlite3.Connection:
    """Open a SQLite file read-only. Accepts a path, or a ``file:`` URI that itself says ``mode=ro`` or ``immutable=1``.

    Point this at a copy. The connection is always opened ``immutable=1``, so SQLite takes no locks and
    creates no ``-wal`` or ``-shm`` file next to the database (a plain ``mode=ro`` open of a WAL-mode file
    does). The cost is that a write-ahead log is ignored, which would make the report silently stale, so a
    database with a non-empty ``-wal`` is refused. A URI that repeats a parameter is refused: SQLite and
    a dict would not have to agree on which value wins.
    """
    text = str(db)
    if text.startswith("file:"):
        head, _, query = text.partition("?")
        pairs = [p.split("=", 1) for p in query.split("&") if p]
        names = [p[0] for p in pairs]
        if len(set(names)) != len(names):
            raise RefusedDb("a file: URI may not repeat a parameter")
        params = {p[0]: (p[1] if len(p) > 1 else "") for p in pairs}
        if params.get("mode") != "ro" and params.get("immutable") != "1":
            raise RefusedDb("a file: URI must say mode=ro (or immutable=1); this tool never opens a database for writing")
        if params.get("immutable", "1") != "1":
            raise RefusedDb("a file: URI that says immutable must say immutable=1")
        path = Path(_unquote_path(head[len("file:"):].removeprefix("//")))
        uri = text if "immutable" in params else text + ("&" if query else "?") + "immutable=1"
    else:
        path = Path(text).resolve()
        if not path.is_file():
            raise RefusedDb(f"{text} is not a file")
        uri = f"file:{_quote_path(str(path))}?mode=ro&immutable=1"
    wal = path.with_name(path.name + "-wal")
    if wal.is_file() and wal.stat().st_size > 0:
        raise RefusedDb(f"{wal.name} is not empty: this report ignores the write-ahead log and would be stale; "
                        "point live-report at a checkpointed copy of the database")
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as exc:
        raise RefusedDb(f"cannot open {text} read-only: {exc}") from exc
    return conn


_OUTCOME_RE = re.compile(r"Evidence-triage proposal \S+ \(confirm [0-9a-f]{8}\): (accepted|modified|no_call)")
_REJECT_RE = re.compile(r"REJECT_([a-z0-9_]+)")
#: Mirrors quant.data.actions.TRIAGE_FACTOR_TOLERANCE (a test keeps the two equal): a human factor this close counts as accepted.
FACTOR_TOLERANCE = 0.02
PRODUCERS = ("engine", "llm")
_OUTCOME_KEYS = ("accepted", "modified", "rejected", "no_call", "pending", "overtaken")
#: What a final human resolution means as a truth label (anchored: the human saw the proposals). ``genuine`` is stored
#: as ``manual_adj`` with factor 1.0, so a ``manual_adj`` with any other factor is a vendor-side adjustment.
_FINAL_TRUTH = {"demerger": "demerger", "scheme": "scheme", "rights": "rights",
                "split": "split_or_bonus_missed_by_vendor", "bonus": "split_or_bonus_missed_by_vendor"}


def _instant(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _norm_kind(kind: Any, factor: Any) -> Any:
    """``genuine`` and ``manual_adj`` with factor 1.0 are the same stored resolution."""
    return "genuine" if kind == "manual_adj" and _num(factor) and abs(factor - 1.0) < 1e-9 else kind


def outcome_vs_final(payload: Mapping[str, Any], kind: Any, factor: Any) -> str:
    """How one proposal compares with a final human resolution: ``accepted``, ``modified`` or ``no_call``.

    The same comparison ``quant.data.actions`` makes for the proposal a human binds: ``no_call`` when the proposal made
    no ``resolution.resolve_kind`` (abstain, wait, unknown), ``modified`` when the kind differs or the factor is more than
    ``FACTOR_TOLERANCE`` away, else ``accepted``. A ``genuine`` proposal with no factor means 1.0.
    """
    res = payload.get("resolution") if isinstance(payload, Mapping) else None
    res = res if isinstance(res, Mapping) else {}
    p_kind, p_factor = res.get("resolve_kind"), res.get("factor")
    if not p_kind:
        return "no_call"
    if p_factor is None and p_kind == "genuine":
        p_factor = 1.0
    if _norm_kind(p_kind, p_factor) != _norm_kind(kind, factor):
        return "modified"
    if not (_num(p_factor) and p_factor > 0):
        return "modified"
    return "modified" if _num(factor) and abs(factor - p_factor) / p_factor > FACTOR_TOLERANCE else "accepted"


def _truth_label(kind: Any, factor: Any) -> str | None:
    kind = _norm_kind(kind, factor)
    if kind == "genuine":
        return "genuine_event"
    if kind == "manual_adj":
        return "vendor_error_other"
    return _FINAL_TRUTH.get(kind)


def live_report(db: Path | str) -> dict[str, Any]:
    """T2: real proposals and human decisions, read through a read-only connection. Advisory until n is about 30.

    Every metric is split per producer (``engine`` and ``llm``) and nothing is pooled: the engine's accept rate says
    nothing about the agent's. Per producer:

    * ``outcomes``: ``accepted`` / ``modified`` (the human resolved the key through this proposal and kept it or changed
      it), ``rejected``, ``no_call`` (the human resolved through this proposal but it made no resolve call: an abstention),
      ``pending`` (the key is still open), ``overtaken`` (the key was resolved through the other producer's proposal).
    * ``accept_rate`` and ``override_rate``: over accepted + modified + rejected. ``no_call``, ``pending`` and
      ``overtaken`` are in neither the numerator nor the denominator: nothing was accepted or overridden.
    * ``dangerous_errors``: each proposal that made a call, scored with ``is_dangerous`` against the human's final
      resolution of its key. A key with no resolution is ``unresolved``, not scored: unknown is not zero.
    * ``added_value_on_engine_abstained``: llm only. Keys where the engine abstained, and what the llm called there.
    """
    conn = open_readonly(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        rows: list[dict[str, Any]] = []
        if {"proposals", "decisions", "runs"} <= tables:
            cur = conn.execute(
                "SELECT p.proposal_id, p.subject_id, p.rule_id, p.status, p.payload_json, p.llm_review, p.decided_on, "
                "p.decision_id, r.started_at, d.context, d.options_json FROM proposals p "
                "LEFT JOIN runs r ON r.run_id = p.created_run_id "
                "LEFT JOIN decisions d ON d.decision_id = p.decision_id "
                "WHERE p.rule_id LIKE 'TRIAGE\\_%' ESCAPE '\\' ORDER BY p.rowid")
            names = [c[0] for c in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    def load(text: Any) -> Any:
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

    def payload(r: Mapping[str, Any]) -> Mapping[str, Any]:
        p = load(r["payload_json"])
        return p if isinstance(p, dict) else {}

    live = [r for r in rows if r["status"] != "superseded"]
    by_rule = {"engine": T.RULE_ID_ENGINE, "llm": T.RULE_ID_AGENT}
    # The human's final resolution per key: the decision bound to an approved proposal of that key.
    final: dict[str, tuple[Any, Any]] = {}
    for r in live:
        if r["status"] == "approved":
            opts = load(r["options_json"])
            first = opts[0] if isinstance(opts, list) and opts and isinstance(opts[0], dict) else None
            if first is not None:
                final[r["subject_id"]] = (first.get("kind"), first.get("factor"))
    label_of = {(r["subject_id"], who): (payload(r).get("classification") or {}).get("label")
                for who, rule in by_rule.items() for r in live if r["rule_id"] == rule}

    def block(who: str) -> dict[str, Any]:
        rs = [r for r in live if r["rule_id"] == by_rule[who]]
        cls = [payload(r).get("classification") or {} for r in rs]
        flags: Counter = Counter()
        for r in rs:
            flags.update(f for f in str(r["llm_review"] or "").split(",") if f and not f.startswith("REJECT_"))
        outcomes: Counter = Counter({k: 0 for k in _OUTCOME_KEYS})
        reasons: Counter = Counter()
        minutes: list[float] = []
        for r in rs:
            st = r["status"]
            if st == "proposed":
                outcomes["overtaken" if r["subject_id"] in final else "pending"] += 1
            elif st == "rejected":
                outcomes["rejected"] += 1
                m = _REJECT_RE.search(str(r["llm_review"] or ""))
                reasons[m.group(1) if m else "unspecified"] += 1
            elif st == "approved":
                m = _OUTCOME_RE.search(str(r["context"] or ""))
                outcomes[m.group(1) if m else "approved_unlabelled"] += 1
            else:
                outcomes[st] += 1
            if st in ("approved", "rejected"):
                start, end = _instant(r["started_at"]), _instant(r["decided_on"])
                if start is not None and end is not None:
                    minutes.append(round((end - start).total_seconds() / 60.0, 2))
        decided = outcomes["accepted"] + outcomes["modified"] + outcomes["rejected"]
        danger, scored, unresolved = [], 0, 0
        for r, c in zip(rs, cls):
            pl = payload(r)
            if c.get("label") in (None, "unknown"):
                continue                                                    # an abstention is never a dangerous call
            if r["subject_id"] not in final:
                unresolved += 1
                continue
            scored += 1
            res = pl.get("resolution") if isinstance(pl.get("resolution"), dict) else {}
            truth = _truth_label(*final[r["subject_id"]])
            kind = is_dangerous({"label": c.get("label"), "confidence": c.get("confidence"), "action": res.get("action"),
                                 "factor": res.get("factor")}, truth) if truth else None
            if kind:
                danger.append({"proposal_id": r["proposal_id"], "key": str(r["subject_id"]).removeprefix("triage:"),
                               "kind": kind})
        non_abstain = sum(1 for c in cls if c.get("label") != "unknown")
        out: dict[str, Any] = {
            "n": len(rs), "by_label": dict(Counter(c.get("label") for c in cls)),
            "by_confidence": dict(Counter(c.get("confidence") for c in cls)), "flags": dict(flags),
            "outcomes": dict(outcomes), "rejection_reasons": dict(reasons),
            "accept_rate": rate_block(outcomes["accepted"], decided),
            "override_rate": rate_block(outcomes["modified"] + outcomes["rejected"], decided),
            "coverage_non_abstain": rate_block(non_abstain, len(rs)),
            "review_minutes": {"n": len(minutes), "min": min(minutes) if minutes else None,
                               "median": statistics.median(minutes) if minutes else None,
                               "max": max(minutes) if minutes else None},
            "dangerous_errors": {"count": len(danger), "cases": danger, "scored": scored, "unresolved": unresolved},
            "claim_status": {"decided_calls": decided, "live_n_needed": max(0, LIVE_CLAIM_N - decided),
                             "enough_for_claim": decided >= LIVE_CLAIM_N},
        }
        out["added_value_on_engine_abstained"] = _added_value(rs, payload, final, label_of) if who == "llm" else {
            "applicable": False, "reason": "the engine cannot add value on its own abstentions; this is the llm's measure"}
        return out

    blocks = {who: block(who) for who in PRODUCERS}
    return {
        "schema": REPORT_SCHEMA, "tier": "t2_live", "advisory": True,
        "status": "ok" if any(b["n"] for b in blocks.values()) else "no_triage_proposals",
        "by_producer": blocks, "superseded": sum(1 for r in rows if r["status"] == "superseded"),
        "caveats": ["Live labels are anchored: the human saw the proposals, so agreement and the final-resolution truth "
                    "flatter both producers. Label 20% blind first.",
                    "no_call (a human resolved a key through a proposal that made no resolve call) is a third outcome. It is "
                    "kept out of the accept and override rates, as are pending and overtaken proposals.",
                    "Metrics are per producer and never pooled. A dangerous error is scored only on keys a human has resolved; "
                    "the rest are listed as unresolved, not as zero.",
                    f"Treat live accuracy as advisory until about {LIVE_CLAIM_N} decided calls per producer; 27 of 30 has a "
                    "Wilson lower bound near 74%."],
    }


def _added_value(llm_rows: Sequence[Mapping[str, Any]], payload, final: Mapping[str, tuple[Any, Any]],
                 label_of: Mapping[tuple[str, str], Any]) -> dict[str, Any]:
    """What the llm called on keys where the engine's own proposal abstained (label ``unknown``)."""
    abstained = {sid for (sid, who), lab in label_of.items() if who == "engine" and lab == "unknown"}
    keys = [r for r in llm_rows if r["subject_id"] in abstained]
    called = [r for r in keys if (payload(r).get("classification") or {}).get("label") not in (None, "unknown")]
    resolved = [r for r in called if r["subject_id"] in final]
    agreed = [r for r in resolved if outcome_vs_final(payload(r), *final[r["subject_id"]]) == "accepted"]
    return {"applicable": True, "engine_abstained_keys": len(abstained), "llm_called": len(called),
            "resolved": len(resolved), "unresolved": len(called) - len(resolved),
            "agreed_with_human": rate_block(len(agreed), len(resolved))}


# ------------------------------------------------------------------ command line

class _Parser(argparse.ArgumentParser):
    def error(self, message: str):                     # exit 1, not argparse's 2: 2 means "gate failed" here
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(1)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="triage_eval.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)

    def common(sp, *, golden: bool = True):
        sp.add_argument("--json", action="store_true", help="print the report as JSON")
        if golden:
            sp.add_argument("--golden", default=str(GOLDEN_DIR), help="golden set directory")

    run = sub.add_parser("run", help="score the golden set (dev split only)")
    run.add_argument("--set", dest="set_name", choices=SET_NAMES, default="dev")
    run.add_argument("--tier", choices=TIERS, required=True)
    run.add_argument("--day", choices=DAYS, default="day0")
    common(run)
    ho = sub.add_parser("holdout", help="report the holdout split, once")
    ho.add_argument("--once", action="store_true", help="acknowledge that the holdout is reported a single time")
    common(ho)
    sl = sub.add_parser("score-llm", help="replay recorded agent runs through the validator")
    sl.add_argument("--runs", default=str(RUNS_DIR))
    sl.add_argument("--rebuild-examples", action="store_true",
                    help="first rewrite the prompt and pack hashes of the hand-written example runs")
    sl.add_argument("--allow-pack-mismatch", action="store_true",
                    help="score runs whose embedded pack differs from the replay world's (shown as a warning)")
    sl.add_argument("--fail-on-stale-prompt", action="store_true",
                    help="treat a run recorded under another prompt as a problem (exit 2)")
    common(sl)
    lv = sub.add_parser("live-report", help="read proposals and decisions from a read-only database")
    lv.add_argument("--db", required=True, help="path to a database copy, or a file: URI with mode=ro")
    common(lv, golden=False)
    mf = sub.add_parser("manifest", help="check or regenerate MANIFEST.json")
    g = mf.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--write", action="store_true")
    mf.add_argument("--label-rev", type=int)
    mf.add_argument("--golden-version", type=int)
    common(mf)
    return p


def _emit(obj: Any, as_json: bool, text: str) -> None:
    sys.stdout.write(_dump(obj) if as_json else text)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        if args.cmd == "run":
            if args.set_name != "dev":
                raise GoldenError(f"run is for the dev split only; the {args.set_name} split is reported once, "
                                  "by 'holdout --once'")
            rep = run_report(load_golden(args.golden), set_name="dev", tier=args.tier, day=args.day)
            _emit(rep, args.json, format_report(rep))
            return 0 if rep["gates"]["passed"] else 2
        if args.cmd == "holdout":
            if not args.once:
                sys.stderr.write("holdout: pass --once; the holdout split is reported a single time\n")
                return 1
            golden = load_golden(args.golden)
            rep = holdout_report(golden)                      # refuses a second look
            record_holdout_look(golden, rep)
            text = ("".join(format_report(r) for r in rep["reports"].values())
                    + f"holdout report sha256 {rep['report_sha256']}\n"
                    + f"commit {HOLDOUT_MARKER_NAME} and MANIFEST.json now: the holdout is spent\n")
            _emit(rep, args.json, text)
            return 0 if all(r["gates"]["passed"] for r in rep["reports"].values()) else 2
        if args.cmd == "score-llm":
            if not Path(args.runs).is_dir():
                raise GoldenError(f"--runs {args.runs} is not a directory")
            golden = load_golden(args.golden)
            if args.rebuild_examples:
                rebuilt = rebuild_examples(args.runs, golden)
                sys.stderr.write(f"rebuilt {len(rebuilt['rewritten'])} hand-written example run(s)\n")
            rep = replay_runs(args.runs, golden, allow_pack_mismatch=args.allow_pack_mismatch,
                              fail_on_stale_prompt=args.fail_on_stale_prompt)
            m = rep["metrics"]
            text = (f"replayed {m['validator_pass']['n']} recorded run(s), {rep['model_calls']} model calls\n"
                    f"validator pass {m['validator_pass']['k']}/{m['validator_pass']['n']}; "
                    f"dangerous errors {m['dangerous_errors']['count']}; "
                    f"blocked dangerous attempts {m['blocked_dangerous_attempts']['count']}; "
                    f"unreadable {rep['problems']['unreadable']}, unknown case {rep['problems']['unknown_case']}, "
                    f"pack mismatch {rep['problems']['pack_mismatch']}\n"
                    f"warnings: stale prompt {rep['warnings']['stale_prompt']}, "
                    f"pack mismatch allowed {rep['warnings']['pack_mismatch_allowed']}\n")
            _emit(rep, args.json, text)
            # Nothing replayed, or a file that could not be read or names no golden case, is not a pass.
            clean = m["validator_pass"]["n"] > 0 and not any(rep["problems"].values())
            return 0 if m["dangerous_errors"]["count"] == 0 and clean else 2
        if args.cmd == "live-report":
            rep = live_report(args.db)
            lines = [f"live report ({rep['status']}, advisory)\n"]
            for who, b in rep["by_producer"].items():
                o, a, d = b["outcomes"], b["accept_rate"], b["dangerous_errors"]
                lines.append(f"  {who}: {b['n']} proposals; accepted {o['accepted']}, modified {o['modified']}, "
                             f"rejected {o['rejected']}, no_call {o['no_call']}, pending {o['pending']}, "
                             f"overtaken {o['overtaken']}; accept {a['k']}/{a['n']} (no_call is not in the rate); "
                             f"dangerous {d['count']} of {d['scored']} scored ({d['unresolved']} unresolved); "
                             f"{b['claim_status']['live_n_needed']} more decided calls before a claim\n")
            _emit(rep, args.json, "".join(lines))
            return 0
        if args.cmd == "manifest":
            if args.check:
                g = load_golden(args.golden)
                _emit({"ok": True, "golden_version": g.manifest["golden_version"], "label_rev": g.manifest["label_rev"]},
                      args.json, f"manifest ok: golden v{g.manifest['golden_version']}, label rev {g.manifest['label_rev']}, "
                                 f"{g.manifest['n_cases']} cases\n")
                return 0
            m = write_manifest(args.golden, golden_version=args.golden_version, label_rev=args.label_rev)
            _emit(m, args.json, f"manifest written: golden v{m['golden_version']}, label rev {m['label_rev']}, "
                                f"{m['n_cases']} cases\n")
            return 0
    except (GoldenError, RefusedDb) as exc:
        sys.stderr.write(f"Refused: {exc}\n")
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
