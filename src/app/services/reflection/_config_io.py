"""Shared file IO + caching helpers for Reflection Room config loaders.

All loaders go through these helpers so a single place owns:
  * file-path resolution (absolute or repo-relative)
  * YAML/JSON parsing
  * cache-clearing for tests

Loaders themselves declare their env var and Pydantic model and call into
``load_yaml_with_model`` / ``load_json_with_model``.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Type, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from ...core.exceptions import ConfigLoadError

T = TypeVar("T", bound=BaseModel)

# Repo root is two parents up from src/app/services/reflection/_config_io.py
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _resolve_path(path_str: str) -> Path:
    """Resolve a config file path. Absolute paths pass through; relative paths
    are resolved against the repo root so the same value works whether the
    process is started from the repo root or from a subdirectory."""
    p = Path(path_str)
    if p.is_absolute():
        return p
    return (_REPO_ROOT / p).resolve()


def env_path(env_var: str, default_relative_path: str) -> str:
    """Read a config-file-path env var, falling back to a default relative
    path resolved against the repo root."""
    return os.getenv(env_var, default_relative_path)


@lru_cache(maxsize=32)
def _read_text(absolute_path: str) -> str:
    """Read a file's text contents. Cached by absolute path."""
    try:
        return Path(absolute_path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigLoadError(f"Config file not found: {absolute_path}") from exc
    except OSError as exc:
        raise ConfigLoadError(f"Failed to read config {absolute_path}: {exc}") from exc


def _parse_yaml(absolute_path: str) -> Dict[str, Any]:
    raw = _read_text(absolute_path)
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"YAML parse error in {absolute_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigLoadError(
            f"Expected mapping at top level of {absolute_path}; got {type(loaded).__name__}"
        )
    return loaded


def _parse_json(absolute_path: str) -> Dict[str, Any]:
    raw = _read_text(absolute_path)
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigLoadError(f"JSON parse error in {absolute_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigLoadError(
            f"Expected object at top level of {absolute_path}; got {type(loaded).__name__}"
        )
    return loaded


def load_yaml_with_model(env_var: str, default_relative_path: str, model: Type[T]) -> T:
    """Load a YAML file via env var and validate against a Pydantic model."""
    path = _resolve_path(env_path(env_var, default_relative_path))
    data = _parse_yaml(str(path))
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigLoadError(f"Schema error in {path}: {exc}") from exc


def load_json_with_model(env_var: str, default_relative_path: str, model: Type[T]) -> T:
    """Load a JSON file via env var and validate against a Pydantic model."""
    path = _resolve_path(env_path(env_var, default_relative_path))
    data = _parse_json(str(path))
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigLoadError(f"Schema error in {path}: {exc}") from exc


def clear_all_loader_caches() -> None:
    """Clear all module-level lru_cache instances declared by individual loaders.

    Loaders register their cache_clear callables here at import time. Used by
    tests to reset state between cases (e.g., when temporarily pointing a
    loader at a fixture path via env override).
    """
    _read_text.cache_clear()
    for clear in _registered_clears:
        clear()


_registered_clears: list = []


def register_clear(clear_callable) -> None:
    """Register a per-loader cache_clear function for clear_all_loader_caches."""
    _registered_clears.append(clear_callable)
