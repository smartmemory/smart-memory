"""Wrapper paths for the single MCP-owned protected Windows credential store."""

from pathlib import Path

from smartmemory_mcp import windows_credentials as _store


def key_path() -> Path:
    return _store.key_path()


def legacy_key_path() -> Path:
    return _store.legacy_key_path()


def read_key() -> str:
    return _store.read_key(key_path(), legacy=legacy_key_path())


def store_key(key: str) -> None:
    _store.store_key(key_path(), key, legacy=legacy_key_path())
