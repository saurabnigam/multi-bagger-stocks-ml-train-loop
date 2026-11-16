"""The suite must never resolve a mutable path to the repository's real stores."""
from pathlib import Path

from quant.config import load as load_config

REPO = Path(__file__).resolve().parents[2]


def test_default_config_paths_never_point_into_the_repository_during_tests():
    paths = load_config().paths
    for name in ("db", "prices_db", "data_dir", "ui_dir", "knowledge_dir", "archive_dir"):
        resolved = Path(getattr(paths, name)).resolve()
        assert REPO not in resolved.parents and resolved != REPO, f"{name} -> {resolved}"
