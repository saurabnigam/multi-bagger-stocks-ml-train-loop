"""Point-in-time rules for triage evidence (WP2, design section 8).

Cohort as_of 2026-10-30 (Friday). Cutoff = 2026-10-30T18:29:59.999999Z = 23:59:59.999999 IST.
Everything is offline: disposable state database under tmp_path, FrozenClock, no network.
"""
from __future__ import annotations

import inspect
from pathlib import Path
import shutil

import pytest

from quant.config import load as load_config
from quant.db.core import apply_schema, connect
from quant.errors import Refused
from quant.run import knowledge_cutoff
from quant.triage import evidence, pit
from quant.types import Actor, FrozenClock

AS_OF = "2026-10-30"
CUTOFF = "2026-10-30T18:29:59.999999Z"
AFTER = "2026-10-30T18:30:00.000000Z"          # cutoff plus one microsecond
ISIN = "INE000000001"
EX = "2026-10-29"


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("triage_pit") / "template.db"
    conn = connect(path)
    apply_schema(conn, kind="state")
    conn.close()
    return path


class World:
    def __init__(self, root: Path, template: Path):
        shutil.copy(template, root / "state.db")
        self.cfg = load_config().with_paths(
            db=root / "state.db", prices_db=root / "p.db", data_dir=root / "d",
            archive_dir=root / "a", knowledge_dir=root / "k", ui_dir=root / "u")

    def run(self, now: str):
        return RunCtx(self.cfg, now)


def RunCtx(cfg, now):
    from quant.run import RunContext
    return RunContext(as_of=AS_OF, kind="triage", track="live", cfg=cfg, clock=FrozenClock(now),
                      actor=Actor(kind="system", name="pytest"))


@pytest.fixture
def world(tmp_path, template_db):
    return World(tmp_path, template_db)


def fields(**over):
    base = dict(
        provenance="human_attach", subject={"isin": ISIN, "ex_date": EX}, url="https://example.test/x",
        title="Company files demerger circular", published_at="2026-10-28", published_at_basis="human_declared",
        source_class="company", relevance="entity", relevance_basis="token_match",
        excerpt="Demerger circular", entity_match=True)
    base.update(over)
    return base


def capture(world, now, **over):
    """Register one envelope at clock time `now`; return (row, loaded envelope)."""
    with world.run(now) as ctx:
        reg = evidence.register(ctx, fields(**over))
        row = evidence.get_capture(ctx.conn, reg.capture_id)
    return row, evidence.load(row)


# ------------------------------------------------------------------ cutoff and published_at parsing

def test_cutoff_is_the_ist_end_of_day_in_utc():
    assert pit.cutoff_utc(AS_OF) == CUTOFF == knowledge_cutoff(AS_OF)
    assert pit.cutoff_utc("2026-10-31") == "2026-10-31T18:29:59.999999Z"    # a Saturday; only the arithmetic


@pytest.mark.parametrize("text,expected", [
    ("2026-09-23", ("2026-09-23", "date")),
    ("2026-09-23T10:00:00+05:30", ("2026-09-23T04:30:00.000000Z", "timestamp")),
    ("2026-09-23T04:30:00Z", ("2026-09-23T04:30:00.000000Z", "timestamp")),
    ("2026-09-23T04:30:00.5+00:00", ("2026-09-23T04:30:00.500000Z", "timestamp")),
])
def test_normalise_published_at(text, expected):
    assert pit.normalise_published_at(text) == expected


@pytest.mark.parametrize("text", ["", "yesterday", "2026-13-40", "2026-09-23T10:00:00", "23 Sep 2026"])
def test_normalise_published_at_refuses_ambiguous_text(text):
    with pytest.raises(ValueError):
        pit.normalise_published_at(text)


def test_admissible_signature_has_no_pack_or_clock_input():
    params = inspect.signature(pit.admissible).parameters
    assert list(params) == ["envelope", "capture_row", "cutoff", "decisive"]
    assert params["decisive"].kind is inspect.Parameter.KEYWORD_ONLY


# ------------------------------------------------------------------ boundary

def test_capture_exactly_at_the_cutoff_is_admitted(world):
    row, env = capture(world, CUTOFF)
    assert pit.admissible(env, row, CUTOFF, decisive=True) == (True, "ok")


def test_capture_one_microsecond_after_the_cutoff_is_rejected(world):
    row, env = capture(world, AFTER)
    assert pit.admissible(env, row, CUTOFF) == (False, "captured_after_cutoff")
    assert pit.admissible(env, row, CUTOFF, decisive=True) == (False, "captured_after_cutoff")


def test_published_timestamp_exactly_at_the_cutoff_is_admitted(world):
    row, env = capture(world, CUTOFF, published_at=CUTOFF, published_at_basis="source")
    assert env.data["published_at"] == CUTOFF and env.data["published_at_precision"] == "timestamp"
    assert pit.admissible(env, row, CUTOFF, decisive=True) == (True, "ok")


def test_published_timestamp_one_microsecond_after_the_cutoff_is_rejected(world):
    row, env = capture(world, CUTOFF, published_at=AFTER, published_at_basis="source")
    assert pit.admissible(env, row, CUTOFF, decisive=True) == (False, "published_after_cutoff")
    assert pit.admissible(env, row, CUTOFF) == (False, "published_after_cutoff")


def test_date_only_published_at_is_compared_at_date_level_with_as_of(world):
    row, env = capture(world, CUTOFF, published_at="2026-10-30")          # the as_of date itself
    assert env.data["published_at_precision"] == "date"
    assert pit.admissible(env, row, CUTOFF, decisive=True) == (True, "ok")
    row2, env2 = capture(world, CUTOFF, published_at="2026-10-31", title="next day")
    assert pit.admissible(env2, row2, CUTOFF, decisive=True) == (False, "published_after_cutoff")


# ------------------------------------------------------------------ skew

def test_published_after_captured_is_rejected_as_skew(world):
    row, env = capture(world, "2026-10-30T06:00:00.000000Z", published_at="2026-10-30T07:00:00Z",
                       published_at_basis="source")
    assert pit.admissible(env, row, CUTOFF) == (False, "published_after_capture")


def test_published_equal_to_captured_is_admitted(world):
    row, env = capture(world, "2026-10-30T06:00:00.000000Z", published_at="2026-10-30T06:00:00Z",
                       published_at_basis="source")
    assert pit.admissible(env, row, CUTOFF, decisive=True) == (True, "ok")


def test_date_only_published_after_the_capture_date_is_skew(world):
    row, env = capture(world, "2026-10-29T10:00:00.000000Z", published_at="2026-10-30")
    assert pit.admissible(env, row, CUTOFF) == (False, "published_after_capture")


def test_ist_date_is_used_for_the_capture_date(world):
    # 2026-10-29T19:00Z is 2026-10-30 00:30 IST, so a document dated 2026-10-30 is not skewed.
    row, env = capture(world, "2026-10-29T19:00:00.000000Z", published_at="2026-10-30")
    assert pit.admissible(env, row, CUTOFF, decisive=True) == (True, "ok")


# ------------------------------------------------------------------ undated

def test_undated_envelope_is_context_only_and_blocks_decisive_claims(world):
    row, env = capture(world, "2026-10-30T06:00:00.000000Z", published_at=None, published_at_basis=None)
    assert env.data["published_at"] is None                     # never defaulted to the capture time
    assert pit.admissible(env, row, CUTOFF) == (True, "ok_undated")
    assert pit.admissible(env, row, CUTOFF, decisive=True) == (False, "published_at_missing")


# ------------------------------------------------------------------ tampering

def test_tampered_envelope_file_is_rejected(world):
    row, env = capture(world, "2026-10-30T06:00:00.000000Z")
    path = Path(row["archive_path"])
    path.write_bytes(path.read_bytes().replace(b"2026-10-28", b"2026-10-01"))
    tampered = evidence.load(row)
    assert tampered.data["published_at"] == "2026-10-01"
    assert pit.admissible(tampered, row, CUTOFF, decisive=True) == (False, "envelope_hash_mismatch")


def test_tampered_envelope_that_is_no_longer_json_is_rejected(world):
    row, _ = capture(world, "2026-10-30T06:00:00.000000Z")
    Path(row["archive_path"]).write_bytes(b"not json")
    assert pit.admissible(evidence.load(row), row, CUTOFF) == (False, "envelope_hash_mismatch")


def test_a_forged_captured_at_inside_the_file_fails_the_hash(world):
    row, env = capture(world, AFTER)                          # really captured after the cutoff
    path = Path(row["archive_path"])
    forged = path.read_bytes().replace(b'{"', b'{"captured_at":"2026-10-01T00:00:00.000000Z",', 1)
    path.write_bytes(forged)
    loaded = evidence.load(row)
    assert pit.admissible(loaded, row, CUTOFF)[0] is False


def test_a_dict_envelope_must_match_the_archived_content(world):
    row, env = capture(world, "2026-10-30T06:00:00.000000Z")
    assert pit.admissible(dict(env.data), row, CUTOFF) == (True, "ok")
    forged = dict(env.data, published_at="2026-01-01")
    assert pit.admissible(forged, row, CUTOFF) == (False, "envelope_hash_mismatch")


def test_row_shape_problems(world):
    row, env = capture(world, "2026-10-30T06:00:00.000000Z")
    assert pit.admissible(env, None, CUTOFF) == (False, "capture_missing")
    assert pit.admissible(env, dict(row, kind="nifty500"), CUTOFF) == (False, "not_triage_doc")
    assert pit.admissible(env, dict(row, captured_at="garbage"), CUTOFF) == (False, "captured_at_unparseable")


# ------------------------------------------------------------------ the admissible evidence set

def test_post_cutoff_canary_is_absent_from_the_admissible_set(world):
    early, _ = capture(world, "2026-10-30T06:00:00.000000Z", title="early circular")
    at_cut, _ = capture(world, CUTOFF, title="at the cutoff")
    late, _ = capture(world, AFTER, title="canary: after the cutoff")
    with world.run("2026-10-31T06:00:00.000000Z") as ctx:          # replayed a day later
        got = evidence.collect_for_subject(ctx.conn, isin=ISIN, ex_date=EX, cutoff=CUTOFF)
    ids = [i.capture_id for i in got.admitted]
    assert ids == [early["capture_id"], at_cut["capture_id"]]      # ordered by capture time
    assert late["capture_id"] not in ids
    assert dict(got.rejected)[late["capture_id"]] == "captured_after_cutoff"
    assert all(i.captured_at <= CUTOFF for i in got.admitted)


def test_the_admissible_set_uses_the_row_time_not_the_file(world):
    row, _ = capture(world, AFTER, title="canary")
    Path(row["archive_path"]).write_bytes(b"{}")                # file edits cannot move a capture back in time
    with world.run("2026-10-31T06:00:00.000000Z") as ctx:
        got = evidence.collect_for_subject(ctx.conn, isin=ISIN, ex_date=EX, cutoff=CUTOFF)
    assert got.admitted == []


def test_the_admissible_set_filters_by_subject_and_reports_tampering(world):
    a, _ = capture(world, "2026-10-30T06:00:00.000000Z", title="for our subject")
    b, _ = capture(world, "2026-10-30T06:00:01.000000Z", title="other company",
                   subject={"isin": "INE000000002", "ex_date": EX})
    c, _ = capture(world, "2026-10-30T06:00:02.000000Z", title="tampered one")
    p = Path(c["archive_path"])
    p.write_bytes(p.read_bytes().replace(b"tampered", b"forged"))
    with world.run("2026-10-30T07:00:00.000000Z") as ctx:
        got = evidence.collect_for_subject(ctx.conn, isin=ISIN, ex_date=EX, cutoff=CUTOFF)
        other = evidence.collect_for_subject(ctx.conn, isin="INE000000002", ex_date=EX, cutoff=CUTOFF)
    assert [i.capture_id for i in got.admitted] == [a["capture_id"]]
    assert dict(got.rejected)[c["capture_id"]] == "envelope_hash_mismatch"
    assert [i.capture_id for i in other.admitted] == [b["capture_id"]]
    item = got.admitted[0]
    assert item.sha256 == a["sha256"] and item.envelope["title"] == "for our subject" and item.decisive_ok is True


def test_the_admissible_set_marks_undated_items_not_decisive(world):
    capture(world, "2026-10-30T06:00:00.000000Z", published_at=None, published_at_basis=None)
    with world.run("2026-10-30T07:00:00.000000Z") as ctx:
        got = evidence.collect_for_subject(ctx.conn, isin=ISIN, ex_date=EX, cutoff=CUTOFF)
    assert len(got.admitted) == 1 and got.admitted[0].decisive_ok is False
    assert got.admitted[0].reason == "ok_undated"


# ------------------------------------------------------------------ window

def test_assert_open_allows_the_cutoff_and_refuses_one_microsecond_later():
    pit.assert_open(FrozenClock(CUTOFF), AS_OF)
    with pytest.raises(Refused) as ei:
        pit.assert_open(FrozenClock(AFTER), AS_OF)
    assert ei.value.code == "triage_window_closed"
