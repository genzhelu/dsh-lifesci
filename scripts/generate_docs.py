#!/usr/bin/env python
"""Generate `docs/commands.md` and `docs/tools.md` from the command registry.

The Python command registry is the single source of truth for command input
schemas, descriptions, and model-facing tool specs; this script renders the
reference documentation so it can never drift.

Usage::

    PYTHONPATH=python .venv/bin/python scripts/generate_docs.py
    PYTHONPATH=python .venv/bin/python scripts/generate_docs.py --check   # CI drift check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python"))

from lifesci import commands, util  # noqa: E402


def _type_of(schema: dict[str, Any]) -> str:
    if "enum" in schema:
        return "enum(" + "|".join(str(x) for x in schema["enum"]) + ")"
    if "oneOf" in schema:
        return "oneOf"
    if "anyOf" in schema:
        return "anyOf"
    kind = schema.get("type", "any")
    if kind == "array":
        items = schema.get("items") or {}
        return f"array<{_type_of(items)}>"
    return str(kind)


def _default_of(schema: dict[str, Any]) -> str:
    if "default" not in schema:
        return ""
    return f"`{json.dumps(schema['default'], ensure_ascii=False)}`"


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_commands(registry: dict[str, commands.Command]) -> str:
    lines: list[str] = []
    lines.append("# Command reference (`lifesci.*`)")
    lines.append("")
    lines.append(
        "Generated from the Python command registry by `scripts/generate_docs.py`; "
        "do not edit by hand."
    )
    lines.append("")
    lines.append(
        "Every command takes one JSON object and returns one JSON envelope "
        "(`data`, `provenance[]`, `warnings[]`, `human_review[]`; see "
        "[provenance.md](provenance.md)). Agent tools have the same names with dots "
        "replaced by underscores and a `lifesci_` prefix (e.g. `lifesci_seq_fetch`)."
    )
    lines.append("")
    lines.append("## Index")
    lines.append("")
    lines.append("| command | tool | category | summary |")
    lines.append("|---|---|---|---|")
    for name in sorted(registry):
        cmd = registry[name]
        lines.append(
            f"| `{cmd.name}` | `{cmd.tool_name}` | {cmd.category} | {_cell(cmd.summary)} |"
        )
    lines.append("")

    for name in sorted(registry):
        cmd = registry[name]
        lines.append(f"## `{cmd.name}` — {cmd.title}")
        lines.append("")
        lines.append(cmd.description.strip())
        lines.append("")
        lines.append(f"- Tool: `{cmd.tool_name}`")
        lines.append(f"- Category: `{cmd.category}` · side effects: `{cmd.danger}`")
        if cmd.output_schema:
            lines.append(f"- Payload schema: [`schemas/{cmd.output_schema}`](../schemas/{cmd.output_schema})")
        if cmd.timeout_ms:
            lines.append(f"- Recommended tool timeout: {cmd.timeout_ms} ms")
        lines.append("")
        properties = (cmd.input_schema or {}).get("properties") or {}
        required = set((cmd.input_schema or {}).get("required") or [])
        if properties:
            lines.append("| input field | type | required | default | description |")
            lines.append("|---|---|---|---|---|")
            for field_name, field_schema in properties.items():
                lines.append(
                    "| `{name}` | {type} | {req} | {default} | {desc} |".format(
                        name=field_name,
                        type=_cell(_type_of(field_schema)),
                        req="yes" if field_name in required else "no",
                        default=_default_of(field_schema),
                        desc=_cell(field_schema.get("description", "")),
                    )
                )
        else:
            lines.append("_No input fields (pass `{}`)._")
        lines.append("")
        if cmd.examples:
            lines.append("Examples:")
            lines.append("")
            for example in cmd.examples:
                lines.append("```json")
                lines.append(json.dumps(util.json_safe(dict(example)), sort_keys=True, indent=2))
                lines.append("```")
            lines.append("")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_tools(registry: dict[str, commands.Command]) -> str:
    lines: list[str] = []
    lines.append("# Agent tool reference (`lifesci_*`)")
    lines.append("")
    lines.append(
        "Generated from the Python command registry by `scripts/generate_docs.py`. "
        "The TypeScript host plugin registers exactly these tools; parameter schemas are "
        "identical to the CLI input schemas (see [commands.md](commands.md))."
    )
    lines.append("")
    lines.append("| tool | command | timeout | purpose |")
    lines.append("|---|---|---|---|")
    for name in sorted(registry):
        cmd = registry[name]
        timeout = f"{cmd.timeout_ms} ms" if cmd.timeout_ms else "—"
        lines.append(f"| `{cmd.tool_name}` | `{cmd.name}` | {timeout} | {_cell(cmd.summary)} |")
    lines.append("")
    for name in sorted(registry):
        cmd = registry[name]
        lines.append(f"## `{cmd.tool_name}`")
        lines.append("")
        lines.append(cmd.description.strip())
        lines.append("")
        lines.append(f"Maps to `lifesci {cmd.name}`. Parameters are the input schema in "
                     f"[commands.md](commands.md#{cmd.name.replace('.', '')}--"
                     f"{util.slugify(cmd.title)}).")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="only verify the committed docs are current")
    args = parser.parse_args(argv)

    registry = commands.discover()
    outputs = {
        REPO_ROOT / "docs" / "commands.md": render_commands(registry),
        REPO_ROOT / "docs" / "tools.md": render_tools(registry),
    }
    stale: list[str] = []
    for path, content in outputs.items():
        if args.check:
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            if current != content:
                stale.append(str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(f"wrote {path} ({len(content.splitlines())} lines)", file=sys.stderr)
    if stale:
        print("stale documentation: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
