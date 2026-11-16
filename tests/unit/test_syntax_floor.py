"""The code base must stay parseable on Python 3.11 (the supported floor).

Two guards, because ``ast.parse(..., feature_version=(3, 11))`` does not reject every 3.12+ construct on a newer
interpreter: PEP 701 f-strings (the same quote nested inside a replacement field) parse fine on 3.12+ whatever
``feature_version`` says, so a second, tokenizer-based test looks for exactly that construct.
"""
from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted(p for d in ("quant", "scripts") for p in (ROOT / d).rglob("*.py"))


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


def test_there_are_files_to_check():
    assert any(p.name == "ui_export.py" for p in FILES)


@pytest.mark.parametrize("path", FILES, ids=_rel)
def test_parses_at_feature_version_3_11(path):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))


def _delim(token_text: str) -> str:
    body = token_text.lstrip("rRbBuUfF")
    return body[:3] if body[:3] in ('"""', "'''") else body[:1]


def _nested_same_quote(source: str) -> list[int]:
    """Lines of string literals nested in an f-string replacement field with the enclosing delimiter."""
    bad: list[int] = []
    stack: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        name = tokenize.tok_name.get(tok.type, "")
        if name == "FSTRING_START":
            if stack and tok.string.lstrip("rRbBuUfF").startswith(stack[-1]):
                bad.append(tok.start[0])
            stack.append(_delim(tok.string))
        elif name == "FSTRING_END":
            stack.pop()
        elif tok.type == tokenize.STRING and stack and _delim(tok.string).startswith(stack[-1]):
            bad.append(tok.start[0])
    return bad


@pytest.mark.skipif(sys.version_info < (3, 12), reason="older interpreters reject the construct in ast.parse itself")
@pytest.mark.parametrize("path", FILES, ids=_rel)
def test_no_fstring_nests_its_own_quote(path):
    assert _nested_same_quote(path.read_text(encoding="utf-8")) == []


@pytest.mark.skipif(sys.version_info < (3, 12), reason="needs PEP 701 tokenization")
def test_the_detector_sees_the_construct_and_allows_the_legal_forms():
    assert _nested_same_quote('x = f"a {d["k"]} b"\n') == [1]
    assert _nested_same_quote("x = f'a {d['k']} b'\n") == [1]
    assert _nested_same_quote('x = f"a {d[\'k\']} b"\n') == []
    assert _nested_same_quote('x = f"""a {d["k"]} b"""\n') == []
