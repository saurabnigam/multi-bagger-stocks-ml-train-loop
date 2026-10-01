"""Evidence triage: a deterministic, LLM-free review aid for suspected corporate actions.

The package proposes; a human resolves. Nothing here changes a score or a factor value, and
nothing here writes a corporate-action row (resolution stays in ``quant.data.actions``).
Modules are imported explicitly (``from quant.triage import evidence``); this file stays empty
of imports so that importing the package costs nothing.
"""
