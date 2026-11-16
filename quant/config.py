from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any
import os
import tomllib


class ConfigNamespace:
    """Attribute-accessible dictionary for configuration sections."""
    def __init__(self, d: dict[str, Any]):
        for k, v in d.items():
            if isinstance(v, dict):
                setattr(self, k, ConfigNamespace(v))
            else:
                setattr(self, k, v)
        self._data = d

    def to_dict(self) -> dict[str, Any]:
        res = {}
        for k, v in self.__dict__.items():
            if k.startswith("_"):
                continue
            if isinstance(v, ConfigNamespace):
                res[k] = v.to_dict()
            else:
                res[k] = v
        return res

    def items(self):
        attrs = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        return attrs.items()

    def keys(self):
        return [k for k in self.__dict__.keys() if not k.startswith("_")]

    def values(self):
        return [v for k, v in self.__dict__.items() if not k.startswith("_")]

    def __iter__(self):
        return iter(self.keys())

    def __getitem__(self, key: str) -> Any:
        if key in self.__dict__ and not key.startswith("_"):
            return self.__dict__[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.__dict__ and not key.startswith("_"):
            return self.__dict__[key]
        return default

    def __repr__(self) -> str:
        attrs = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        return f"ConfigNamespace({attrs})"


class PathsNamespace:
    """Paths namespace with Path objects resolved relative to repository root."""
    def __init__(self, paths_dict: dict[str, Any], root: Path):
        self._root = root
        for k, v in paths_dict.items():
            p = Path(v)
            if not p.is_absolute():
                p = (root / p).resolve()
            setattr(self, k, p)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def __repr__(self) -> str:
        attrs = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        return f"PathsNamespace({attrs})"


class Config:
    def __init__(self, raw_dict: dict[str, Any], root: Path, policy_hash: str | None = None):
        self._root = root
        self._raw = copy.deepcopy(raw_dict)
        
        for k, v in raw_dict.items():
            if k == "paths":
                setattr(self, "paths", PathsNamespace(v, root))
            elif isinstance(v, dict):
                setattr(self, k, ConfigNamespace(v))
            else:
                setattr(self, k, v)
                
        if policy_hash is None:
            self.policy_sha256 = self._compute_policy_hash(raw_dict)
        else:
            self.policy_sha256 = policy_hash

    @staticmethod
    def _compute_policy_hash(raw_dict: dict[str, Any]) -> str:
        # Policy is everything except [paths]
        policy_dict = {k: v for k, v in sorted(raw_dict.items()) if k != "paths"}
        # Deterministic json serialization
        encoded = json.dumps(policy_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def with_paths(self, **kwargs) -> Config:
        """Create a copy of Config with overridden paths, preserving policy_sha256."""
        new_raw = copy.deepcopy(self._raw)
        paths_dict = new_raw.get("paths", {})
        for k, v in kwargs.items():
            paths_dict[k] = str(v)
        new_raw["paths"] = paths_dict
        return Config(new_raw, self._root, policy_hash=self.policy_sha256)

    def to_dict(self) -> dict[str, Any]:
        res = copy.deepcopy(self._raw)
        res["paths"] = self.paths.to_dict()
        return res


def load(path: str | Path | None = None) -> Config:
    repo_root = Path(__file__).resolve().parents[1]
    if path is None:
        cfg_env = os.environ.get("QUANT_CONFIG_PATH")
        if cfg_env:
            target_path = Path(cfg_env)
        else:
            target_path = repo_root / "config/quant.toml"
    else:
        target_path = Path(path)
        if not target_path.is_absolute():
            target_path = (repo_root / target_path).resolve()
            
    with open(target_path, "rb") as f:
        data = tomllib.load(f)
        
    cfg = Config(data, repo_root)
    overrides = {}
    env_map = {
        "QUANT_DB_PATH": "db",
        "QUANT_PRICES_DB_PATH": "prices_db",
        "QUANT_DATA_DIR": "data_dir",
        "QUANT_UI_DIR": "ui_dir",
        "QUANT_LEGACY_DB_PATH": "legacy_db",
        "QUANT_KNOWLEDGE_DIR": "knowledge_dir",
        "QUANT_ARCHIVE_DIR": "archive_dir",
    }
    for env_key, path_key in env_map.items():
        if os.environ.get(env_key):
            overrides[path_key] = os.environ[env_key]
    if overrides:
        cfg = cfg.with_paths(**overrides)
    return cfg
