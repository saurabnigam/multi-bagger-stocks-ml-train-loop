"""Factor specification and abstract base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Tuple, TYPE_CHECKING
import pandas as pd

if TYPE_CHECKING:
    from quant.factors.inputs import FactorInputs


@dataclass(frozen=True)
class FactorSpec:
    name: str
    version: int
    family: str
    direction: int
    horizon_m: int
    hypothesis: str
    formula: str
    inputs: Tuple[str, ...]
    lookback_days: int
    applies_to_financials: bool
    level: Literal["stock", "sector"]
    backfillable: bool
    min_coverage: float
    evidence: str
    hypothesis_id: str

    @property
    def factor_id(self) -> str:
        return f"{self.name}@{self.version}"


class Factor(ABC):
    """Abstract base class for all quantitative factors."""

    def __init__(self, spec: FactorSpec):
        self.spec = spec

    @property
    def factor_id(self) -> str:
        return self.spec.factor_id

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def version(self) -> int:
        return self.spec.version

    @abstractmethod
    def compute(self, inputs: FactorInputs) -> pd.Series:
        """Compute raw factor values indexed by member security_ids."""
        raise NotImplementedError
