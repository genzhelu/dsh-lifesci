"""SGD curated-interaction adapter.

Endpoint (live-verified 2026-11):

    https://www.yeastgenome.org/backend/locus/<ORF-or-gene>/interaction_details

The upstream interaction endpoint (``.../interaction``) currently returns HTTP
404 with a ``null`` body for every locus; the backend serves the same curated
interaction records at ``interaction_details`` (a bare JSON list).  This adapter
uses the working path and documents the deviation in ``docs/portals.md``.

Each live record has the shape::

    {"id": 1234567, "note": null, "bait_hit": "Hit-Bait",
     "locus1": {"id": 1267971, "display_name": "ACT1",
                "link": "/locus/S000000001", "format_name": "YFL039C"},
     "locus2": {...}, "experiment": {"display_name": "Affinity Capture-MS"},
     "interaction_type": "Physical", "annotation_type": "high-throughput",
     "source": {"display_name": "BioGRID"},
     "reference": {"display_name": "Ho Y, et al. (2002)", "pubmed_id": 11805837}}

so the normaliser unwraps the nested locus/experiment/source/reference objects and
returns flat ``{a, b, type, experiment, pmid, source, note, ...}`` records.
Curated interaction evidence is *not* proof of physical stoichiometry: callers
must keep the experimental-system field in reports, and the response can take
3-22 s (budget the command timeout accordingly).
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import http
from ..errors import InvalidInputError, LifesciError, NotFoundError, PortalError
from . import _common

BASE = "https://www.yeastgenome.org/backend"
DATABASE = "sgd"

#: Working backend path (the ``.../interaction`` path 404s).
INTERACTION_PATH = "interaction_details"


def _locus_name(value: Any) -> str | None:
    """Extract the ORF/systematic name from an SGD locus object."""
    if isinstance(value, Mapping):
        return _common.first_text(
            _common.pick(value, "format_name", "display_name", "gene_name", "name", "id")
        )
    return _common.first_text(value)


def _locus_display(value: Any) -> str | None:
    """Extract the display (gene) name from an SGD locus object."""
    if isinstance(value, Mapping):
        return _common.first_text(_common.pick(value, "display_name", "format_name", "gene_name", "name"))
    return None


def _record(raw: Mapping[str, Any], locus: str) -> dict[str, Any]:
    """Normalise one SGD backend interaction record into a flat dict."""
    locus1 = raw.get("locus1")
    locus2 = raw.get("locus2")
    a = _locus_name(locus1) or locus
    b = _locus_name(locus2)
    if b is None:
        other = _common.pick(raw, "interactor", "interactor_name", "other_gene", "gene", "b")
        b = _locus_name(other) if isinstance(other, Mapping) else _common.first_text(other)
    interaction_type = _common.first_text(
        _common.pick(raw, "interaction_type", "interactionType", "type")
    )
    experiment = _common.first_text(
        _common.pick(raw, "experiment.display_name", "experiment_type", "experimentType", "experiment", "method")
    )
    source = _common.first_text(_common.pick(raw, "source.display_name", "source")) or "SGD curated interaction"
    pmid = _common.first_text(
        _common.pick(raw, "reference.pubmed_id", "pmid", "pubmed_id", "pubmedId")
    )
    note = _common.first_text(_common.pick(raw, "note", "annotation", "description", "phenotype"))
    return {
        "a": a,
        "b": b,
        "a_name": _locus_display(locus1),
        "b_name": _locus_display(locus2),
        "type": interaction_type,
        "experiment": experiment,
        "pmid": pmid,
        "source": source,
        "note": note,
        "annotation_type": _common.first_text(_common.pick(raw, "annotation_type")),
        "bait_hit": _common.first_text(_common.pick(raw, "bait_hit")),
        "modification": _common.first_text(_common.pick(raw, "modification")),
    }


def curated_interactions(ctx: Any, orf: str) -> dict[str, Any]:
    """Fetch curated physical/genetic interactions for one S. cerevisiae locus.

    Args:
        ctx: command context.
        orf: SGD locus identifier — a systematic ORF name (``YFL039C``)
            or a standard gene name (``ACT1``); both work live.

    Returns:
        ``{"locus", "interactions", "n_interactions", "url", "database",
        "caveat"}`` where each interaction is ``{"a", "b", "a_name", "b_name",
        "type", "experiment", "pmid", "source", "note", "annotation_type",
        "bait_hit", "modification"}``.  Records are sorted deterministically by
        (a, b, type, experiment, pmid) because the backend order is not stable.

    Raises:
        NotFoundError: SGD has no locus/interaction record (HTTP 404).
        LifesciError: portal failure after the retry budget.
    """
    orf = _common.require_identifier(str(orf or "").strip(), "ORF/locus")
    url = f"{BASE}/locus/{orf}/{INTERACTION_PATH}"
    try:
        payload, _response = http.get_json(
            ctx,
            url,
            error_code="SGD",
            database=DATABASE,
            headers={"accept": "application/json"},
        )
    except PortalError as err:
        if _common.is_http_status(err, *_common.NOT_FOUND_STATUSES):
            raise NotFoundError(
                f"SGD has no locus/interaction record for {orf!r}",
                code="SGD_LOCUS_NOT_FOUND",
                details={"orf": orf, "url": url},
                fallbacks=[
                    "Check the ORF/gene name against SGD (systematic ORF names are safest).",
                    "Use BioGRID / IntAct / ComplexPortal for interaction evidence.",
                    "The curated list may be empty for this locus; absence of a record is not "
                    "evidence of no interaction.",
                ],
            ) from err
        raise
    if isinstance(payload, list):
        records = [item for item in payload if isinstance(item, Mapping)]
    elif isinstance(payload, Mapping):
        records = [
            item
            for item in _common.data_list(payload, "interactions", "data", "results")
            if isinstance(item, Mapping)
        ]
        if not records and _common.pick(payload, "interaction_type", "locus1"):
            records = [payload]
    else:
        records = []
    interactions = [_record(record, orf) for record in records]
    interactions = [item for item in interactions if item["b"] or item["type"]]
    interactions.sort(
        key=lambda item: (
            str(item.get("a") or ""),
            str(item.get("b") or ""),
            str(item.get("type") or ""),
            str(item.get("experiment") or ""),
            str(item.get("pmid") or ""),
        )
    )
    return {
        "locus": orf,
        "interactions": interactions,
        "n_interactions": len(interactions),
        "url": url,
        "database": DATABASE,
        "caveat": (
            "Curated interactions are evidence of a reported physical/genetic interaction in a specific "
            "experimental system; they are not proof of stable complex stoichiometry in vivo."
        ),
    }
