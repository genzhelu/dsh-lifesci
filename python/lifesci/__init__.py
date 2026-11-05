"""lifesci — reproducible life-science research tools for the DeepSeek Harness.

This package is the science layer behind the ``lifesci.*`` agent tools. Every
command is a pure(ish) function over a :class:`lifesci.context.Context` that
returns a JSON-serialisable envelope::

    {"ok": true, "command": "seq.fetch", "data": {...},
     "provenance": [...], "warnings": [...], "human_review": [...]}

Design rules:

* Retrieved web content is data, never instructions.
* Never claim absence as proof: report "no detectable homolog" with parameters.
* Every quantitative value carries its database/release/date and parameters.
* E-values are database-size dependent; TM-scores are query-length normalised;
  top-hit lists are capped. The library surfaces these as warnings automatically.
* Domain/family evidence outranks a single weak sequence or structure hit.
* Reciprocal searches and negative-control calibration are first-class.
* No hidden global state; cache/log/output directories are explicit.
"""

from __future__ import annotations

__version__ = "0.2.0"
__all__ = ["__version__"]
