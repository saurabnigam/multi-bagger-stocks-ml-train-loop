from typing import Any


class QuantError(Exception):
    """Base exception for all V2 quant engine errors."""
    pass


class Blocked(QuantError):
    """Execution is blocked due to data or gate prerequisites."""
    def __init__(self, code: str, detail: str):
        super().__init__(f"Blocked({code}): {detail}")
        self.code = code
        self.detail = detail


class Refused(QuantError):
    """Execution is refused due to invalid inputs, missing sessions, or policy violations."""
    def __init__(self, code: str, detail: str):
        super().__init__(f"Refused({code}): {detail}")
        self.code = code
        self.detail = detail


class LookaheadError(QuantError):
    """Lookahead / PIT leakage error."""
    def __init__(self, detail: str):
        super().__init__(f"LookaheadError: {detail}")
        self.detail = detail


class ImmutableConflict(QuantError):
    """Attempted overwrite of an immutable row with differing values."""
    def __init__(self, table: str, key: Any):
        super().__init__(f"ImmutableConflict on table '{table}' with key {key}")
        self.table = table
        self.key = key


class SourceChanged(QuantError):
    """External source data changed unexpectedly against recorded manifest."""
    def __init__(self, detail: str):
        super().__init__(f"SourceChanged: {detail}")
        self.detail = detail
