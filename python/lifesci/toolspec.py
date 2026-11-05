"""Export model-facing tool specifications from the command registry.

The Python command registry is the single source of truth for tool names, input
schemas, and descriptions.  The TypeScript host plugin imports the generated
``src/tool-specs.ts``, so the agent-facing schema can never drift from the CLI.

Usage::

    python -m lifesci.toolspec --out src/tool-specs.ts
    python -m lifesci.toolspec --check src/tool-specs.ts   # CI drift check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def build_tool_specs() -> dict[str, Any]:
    from . import __version__, commands

    return {
        "schemaVersion": 1,
        "package": "dsh-lifesci",
        "version": __version__,
        "tools": commands.tool_specs(),
    }


def render_json(specs: dict[str, Any]) -> str:
    return json.dumps(specs, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def render_ts(specs: dict[str, Any]) -> str:
    header = (
        "/* AUTO-GENERATED FILE — do not edit by hand.\n"
        " * Regenerate with: python -m lifesci.toolspec --out src/tool-specs.ts\n"
        " * Source of truth: python/lifesci/commands/*.py\n"
        " */\n"
    )
    body = json.dumps(specs, sort_keys=True, indent=2, ensure_ascii=False)
    return header + "export const TOOL_SPECS = " + body + " as const;\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write to this path (default: stdout)")
    parser.add_argument("--format", choices=["ts", "json"], default="ts")
    parser.add_argument(
        "--check",
        metavar="PATH",
        help="exit 1 if PATH is not exactly the generated output (CI drift check)",
    )
    args = parser.parse_args(argv)

    specs = build_tool_specs()
    rendered = render_ts(specs) if args.format == "ts" else render_json(specs)

    if args.check:
        target = Path(args.check)
        if not target.is_file():
            print(f"lifesci.toolspec: missing {target}", file=sys.stderr)
            return 1
        if target.read_text(encoding="utf-8") != rendered:
            print(
                f"lifesci.toolspec: {target} is stale; run "
                f"'python -m lifesci.toolspec --out {target}'",
                file=sys.stderr,
            )
            return 1
        print(f"lifesci.toolspec: {target} is up to date")
        return 0

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        print(f"lifesci.toolspec: wrote {len(specs['tools'])} tool specs to {target}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
