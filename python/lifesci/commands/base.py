"""Command registry primitives.

A :class:`Command` bundles everything needed to expose one operation as both a
CLI command and an agent tool:

* ``name`` — dotted CLI name (``seq.fetch``);
* ``tool_name`` — model-facing snake_case tool name (``lifesci_seq_fetch``);
* ``title``/``summary``/``description`` — one-line human title, one-line summary,
  and the model-facing description (must mention scientific caveats);
* ``input_schema`` — JSON Schema for the JSON input (also becomes the tool's
  parameter schema, so CLI and tool can never drift);
* ``output_schema`` — payload schema file name under ``schemas/`` (optional);
* ``handler(input_dict, ctx) -> data`` — the implementation.

Command modules register themselves with :func:`register` (or the
:func:`command` decorator); :mod:`lifesci.commands` discovers modules lazily.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .. import util
from ..context import Context
from ..errors import UsageError


@dataclass(frozen=True)
class Command:
    """Descriptor + handler for one lifesci operation."""

    name: str
    tool_name: str
    title: str
    summary: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[[dict[str, Any], Context], Any]
    output_schema: str | None = None
    category: str = "general"
    timeout_ms: int | None = None
    examples: tuple[Mapping[str, Any], ...] = ()
    danger: str = "read"  # read | write | network | compute

    def run(self, args: Mapping[str, Any] | None, ctx: Context) -> Any:
        payload = dict(args or {})
        return self.handler(payload, ctx)

    def tool_spec(self) -> dict[str, Any]:
        """Model-facing tool specification (consumed by the TypeScript host)."""
        spec: dict[str, Any] = {
            "command": self.name,
            "name": self.tool_name,
            "title": self.title,
            "description": self.description,
            "parameters": util.json_safe(dict(self.input_schema)),
            "category": self.category,
        }
        if self.timeout_ms is not None:
            spec["timeoutMs"] = int(self.timeout_ms)
        if self.examples:
            spec["examples"] = [util.json_safe(dict(e)) for e in self.examples]
        return spec


REGISTRY: dict[str, Command] = {}
_TOOL_NAMES: dict[str, str] = {}


def make_tool_name(command_name: str) -> str:
    return "lifesci_" + command_name.replace(".", "_").replace("-", "_")


_COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


def register(command_obj: Command) -> Command:
    """Register a command, rejecting duplicates and malformed names."""
    name = command_obj.name
    if not name or not _COMMAND_NAME_RE.fullmatch(name):
        raise UsageError(
            f"invalid command name {name!r}: expected lower-case dotted name "
            "(letters/digits/underscores, non-empty dot-separated segments)"
        )
    if name in REGISTRY:
        raise UsageError(f"duplicate command registration: {name}")
    if command_obj.tool_name in _TOOL_NAMES:
        raise UsageError(
            f"duplicate tool name {command_obj.tool_name} for {name} "
            f"(already used by {_TOOL_NAMES[command_obj.tool_name]})"
        )
    REGISTRY[name] = command_obj
    _TOOL_NAMES[command_obj.tool_name] = name
    return command_obj


def command(
    *,
    name: str,
    title: str,
    summary: str,
    description: str,
    input_schema: Mapping[str, Any],
    output_schema: str | None = None,
    category: str = "general",
    timeout_ms: int | None = None,
    examples: tuple[Mapping[str, Any], ...] = (),
    danger: str = "read",
) -> Callable[[Callable[[dict[str, Any], Context], Any]], Command]:
    """Decorator form of :func:`register`."""

    def decorator(handler: Callable[[dict[str, Any], Context], Any]) -> Command:
        return register(
            Command(
                name=name,
                tool_name=make_tool_name(name),
                title=title,
                summary=summary,
                description=description,
                input_schema=input_schema,
                handler=handler,
                output_schema=output_schema,
                category=category,
                timeout_ms=timeout_ms,
                examples=examples,
                danger=danger,
            )
        )

    return decorator


#: Common JSON-schema fragments reused by command input schemas.
EMPTY_INPUT: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": {}}
