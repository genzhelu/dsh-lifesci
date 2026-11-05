"""Filesystem locations for the lifesci science layer.

The package intentionally carries no environment-specific absolute paths.  All
locations are resolved from the installed package position or from environment
variables, so the same checkout works for a developer, a CI runner, and an
installed DeepSeek Harness plugin.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Repository root (the checkout that holds ``schemas/`` and ``python/``).
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]

#: Schemas are kept at the repository root and also shipped
#: inside the package for pip-installed use.
SCHEMA_DIRS = (REPO_ROOT / "schemas", PACKAGE_DIR / "schemas")

#: Recorded test/support data shipped with the repository.
FIXTURES_DIR = REPO_ROOT / "fixtures"

#: Optional static binaries (foldseek). Never auto-downloaded at import time.
TOOLS_DIR = REPO_ROOT / "tools" / "bin"


def schema_path(name: str) -> Path:
    """Resolve ``schemas/<name>`` from the repository or the installed package.

    Raises:
        FileNotFoundError: the schema is missing from every search location.
    """
    for base in SCHEMA_DIRS:
        candidate = base / name
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(b) for b in SCHEMA_DIRS)
    raise FileNotFoundError(f"schema {name!r} not found (searched: {searched})")


def default_cache_dir(cwd: str | os.PathLike[str] | None = None) -> Path:
    """Choose a writable-by-default cache directory.

    Priority:

    1. ``$LIFESCI_CACHE_DIR`` (explicit override, always wins).
    2. ``<cwd>/.lifesci-cache`` — the agent's session workspace, so sandboxed
       runs never need to escape their writable roots.
    3. ``$DSH_HOME/cache/lifesci`` when the harness home is known.
    4. ``$XDG_CACHE_HOME/lifesci`` or ``~/.cache/lifesci``.
    """
    explicit = os.environ.get("LIFESCI_CACHE_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()

    base = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
    if os.access(base, os.W_OK):
        return base / ".lifesci-cache"

    dsh_home = os.environ.get("DSH_HOME")
    if dsh_home:
        return Path(dsh_home).expanduser() / "cache" / "lifesci"

    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "lifesci"
    return Path.home() / ".cache" / "lifesci"


def default_log_path(cache_dir: Path) -> Path:
    """Provenance JSONL log lives beside the cache unless overridden."""
    override = os.environ.get("LIFESCI_LOG")
    if override:
        return Path(override).expanduser().resolve()
    return cache_dir / "provenance.jsonl"


def default_datasets_file(cache_dir: Path) -> Path:
    """Registry of locally registered read-only datasets."""
    override = os.environ.get("LIFESCI_DATASETS")
    if override:
        return Path(override).expanduser().resolve()
    return cache_dir / "datasets.json"
