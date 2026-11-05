"""Shared helpers for the generic golden suite.

The golden tests are *command-level* checks: they call
:func:`lifesci.cli.run_command` (the same code path as the CLI), assert the
envelope succeeded, and validate the payload against the shipped JSON schema.
All inputs are synthetic and written under ``tmp_path`` — the suite never reads
repository fixtures and never touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from lifesci import envelope as envelope_mod
from lifesci.cli import run_command


def run_golden(
    ctx: Any,
    command: str,
    args: Mapping[str, Any],
    *,
    schema: str | None = None,
) -> dict[str, Any]:
    """Run one command offline, assert success, and optionally validate its payload.

    ``schema`` is a filename under ``schemas/`` (e.g. ``"stats.schema.json"``).
    Input validation is always on (``validate=True``), so the tests also cover
    the strict command input schemas.
    """
    envelope = run_command(command, args, ctx=ctx, validate=True)
    assert envelope["ok"] is True, f"{command} failed: {envelope.get('error')}"
    if schema is not None:
        errors = envelope_mod.validate_payload(envelope["data"], schema)
        assert errors == [], f"{command} payload violates {schema}: {errors[:5]}"
    return envelope


def warning_codes(envelope: Mapping[str, Any]) -> list[str]:
    """Machine-readable warning codes on an envelope, in insertion order."""
    return [str(entry.get("code")) for entry in envelope.get("warnings") or []]


def assert_warning(envelope: Mapping[str, Any], code: str) -> dict[str, Any]:
    """Return the warning entry ``code`` or fail with the codes that were reported."""
    for entry in envelope.get("warnings") or []:
        if entry.get("code") == code:
            return entry
    raise AssertionError(f"expected warning {code!r}; reported codes: {warning_codes(envelope)}")


def write_text(path: Path, text: str) -> Path:
    """Write UTF-8 text, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_fasta(path: Path, records: list[tuple[str, str]]) -> Path:
    """Write ``[(name, sequence)]`` as FASTA."""
    return write_text(path, "".join(f">{name}\n{seq}\n" for name, seq in records))


def write_json(path: Path, payload: Any) -> Path:
    """Write JSON deterministically."""
    return write_text(path, json.dumps(payload, sort_keys=True, indent=2) + "\n")


def build_tiny_mmcif(path: Path) -> Path:
    """Write a tiny synthetic protein-like mmCIF with known inter-chain distances.

    Chains/residues (all single ``CA`` atoms on the x-axis):

    * chain ``A``: residues 1 and 2 at x = 0.0 and 1.5;
    * chain ``B``: residue 1 at x = 3.0;
    * chain ``C``: residue 1 at x = 20.0 (far away, used as a negative control).

    The nearest A/B atom pair is therefore A:2-B:1 at 1.5 Å. ``gemmi`` is an
    optional dependency of the science layer; callers use
    ``pytest.importorskip("gemmi")`` and skip cleanly when it is not installed.

    Note: residues are added to a chain *before* the chain is attached to the
    model; attaching an empty chain first makes gemmi's mmCIF writer omit the
    atom records for subsequently added residues.
    """
    import gemmi

    structure = gemmi.Structure()
    model = gemmi.Model("1")
    for chain_name, residues in (
        ("A", [(1, 0.0), (2, 1.5)]),
        ("B", [(1, 3.0)]),
        ("C", [(1, 20.0)]),
    ):
        chain = gemmi.Chain(chain_name)
        for seqid, x_coord in residues:
            residue = gemmi.Residue()
            residue.name = "GLY"
            residue.seqid = gemmi.SeqId(seqid, " ")
            atom = gemmi.Atom()
            atom.name = "CA"
            atom.element = gemmi.Element("C")
            atom.pos = gemmi.Position(x_coord, 0.0, 0.0)
            residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
    structure.add_model(model)
    structure.make_mmcif_document().write_file(str(path))
    return path
