"""Portal adapters for the lifesci science layer.

One module per public portal.  Every adapter:

* accepts a :class:`lifesci.context.Context` as its first argument and returns
  plain JSON-serialisable Python data (dicts/lists) parsed from the portal
  payload — raw payload access stays inside the adapter;
* performs all HTTP through :mod:`lifesci.http` (never ``urllib`` directly), so
  caching, retries, rate limiting, offline replay and provenance logging apply
  uniformly;
* raises :class:`lifesci.errors.LifesciError` subclasses with stable codes and
  actionable ``fallbacks`` instead of leaking transport/parser exceptions;
* records the database release (``database=``/``release_header=`` arguments to
  ``http`` or explicit parsing) and calls ``ctx.warn`` for every fallback or
  documented quirk, so a result never silently changes source.

The modules are import-cheap: heavy or optional libraries (pyhmmer, gemmi,
biopython) are imported lazily inside the functions that need them, and no
module performs network or filesystem I/O at import time.  ``download_model``
adjacent fallbacks (e.g. AlphaFold DB → ESMFold) live in the command layer, not
here: each adapter stays single-portal.

Module exports are **lazy** (PEP 562): ``from lifesci.adapters import uniprot``
imports only that module, and ``lifesci.adapters.__all__`` still lists the whole
set so the doctor/plugin tooling can enumerate them without loading pyhmmer or
gemmi.  This also keeps the package importable while a workstream is still
landing a module.
"""

from __future__ import annotations

import importlib

#: Every adapter module in this package (one per portal plus the shared helper).
ADAPTER_MODULES: tuple[str, ...] = (
    "_common",
    "uniprot",
    "ncbi",
    "europepmc",
    "interpro",
    "alphafold",
    "esmfold",
    "foldseek",
    "pdb",
    "sgd",
    "oma",
    "orthodb",
    "local_dataset",
    "remote",
)

__all__: list[str] = list(ADAPTER_MODULES)


def __getattr__(name: str) -> object:
    """Lazily import an adapter submodule on first attribute access (PEP 562)."""
    if name in ADAPTER_MODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Expose the lazy adapter modules to ``dir()`` / tooling discovery."""
    return sorted(set(list(globals()) + list(ADAPTER_MODULES)))


def available_modules() -> dict[str, bool]:
    """Report which adapter modules are importable in this checkout.

    Returns:
        ``{module_name: importable}`` sorted by name.  Modules owned by another
        workstream are reported as unavailable instead of raising, which lets
        ``doctor`` describe a partially built plugin without failing.
    """
    status: dict[str, bool] = {}
    for name in sorted(ADAPTER_MODULES):
        if name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception:  # noqa: BLE001 - a missing/broken module is a reportable state
            status[name] = False
        else:
            status[name] = True
    return status
