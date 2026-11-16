from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd
    from quant.config import Config


@dataclass(frozen=True)
class Actor:
    kind: Literal["human", "llm", "system"]
    name: str

    @property
    def by(self) -> str:
        return f"{self.kind}:{self.name}"


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Returns an aware UTC datetime."""
        raise NotImplementedError

    def iso(self) -> str:
        """Returns ISO 8601 UTC string with microseconds and Z suffix."""
        dt = self.now()
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def now_iso(self) -> str:
        """Alias for iso()."""
        return self.iso()


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock(Clock):
    def __init__(self, time_val: str | datetime):
        if isinstance(time_val, str):
            # Parse ISO string, replace Z with +00:00 for fromisoformat if needed
            cleaned = time_val.strip()
            if cleaned.endswith("Z"):
                cleaned = cleaned[:-1] + "+00:00"
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            self._dt = dt
        elif isinstance(time_val, datetime):
            if time_val.tzinfo is None:
                self._dt = time_val.replace(tzinfo=timezone.utc)
            else:
                self._dt = time_val.astimezone(timezone.utc)
        else:
            raise TypeError(f"Expected str or datetime, got {type(time_val)}")

    def now(self) -> datetime:
        return self._dt

    def advance(self, **kwargs) -> datetime:
        from datetime import timedelta
        self._dt = self._dt + timedelta(**kwargs)
        return self._dt


@dataclass
class Result:
    status: str
    counts: dict[str, int] = field(default_factory=dict)
    details: dict[str, object] = field(default_factory=dict)


@dataclass
class Check:
    id: str
    status: Literal["PASS", "FAIL", "DEFERRED"]
    observed: object
    expected: object
    reason: str
    blocking: bool


@dataclass
class CheckReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        # no FAIL on blocking checks
        return not any(c.status == "FAIL" for c in self.checks if c.blocking)

    @property
    def deferred(self) -> list[str]:
        return [c.id for c in self.checks if c.status == "DEFERRED"]


@dataclass
class HacResult:
    mean: float | None
    se: float | None
    t: float | None
    ci_lo: float | None
    ci_hi: float | None
    n: int
    n_eff: float
    status: str


@dataclass
class CriteriaCheck:
    subject_id: str
    checks: list[Check]
    eligible: bool
    evidence_ids: list[int]
    next_review: str | None


@dataclass
class World:
    db_path: Path
    prices_db_path: Path
    cfg: Any  # Config
    sessions: Any  # pd.DataFrame
    months: list[str]
    security_ids: list[int]
    events: dict[str, object]


@dataclass
class Draft:
    cohort_id: str
    as_of: str
    track: str
    knowledge_cutoff: str
    definition_hash: str
    members: Any = None  # pd.DataFrame indexed by security_id
    groups: Any = None  # pd.Series security_id -> sector_group
    source_refs: dict = field(default_factory=dict)
    factor_values: Any = None  # pd.DataFrame (staging rows)
    model_weights: Any = None  # pd.DataFrame
    scores: Any = None  # pd.DataFrame
    membership_hash: str = ""
