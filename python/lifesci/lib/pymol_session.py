"""PyMOL session generation (``.pml``) with optional headless validation.

The plugin must produce a **session script** even when PyMOL is not installed:
the ``.pml`` is the reproducible deliverable, and rendering is only claimed when
PyMOL was actually executed (``pymol -cq <script>``) and the expected artefacts
exist.  Absence of PyMOL therefore yields ``validation.status == "SKIPPED"`` plus
a ``PYMOL_MISSING`` warning — never a silent success.

Headless rendering quirk
------------------------
A full-session ``cmd.png`` can silently produce nothing headless for a very
large session; export small extracted structures instead and verify that the PNG
exists and is non-empty.  When a render is requested this module records the
``PNG_RENDER_LIMITATION`` warning.

Supported ``spec`` (opaque to command owners; documented here)::

    {
      "name": "example_session",            # session/file stem (default "session")
      "objects": [                            # loaded structures/models
        {"path": "models/example.cif",        # required
         "name": "model_a",                   # object name (default file stem)
         "load_type": "cif",                  # optional PyMOL load type
         "color": "grey70",                   # optional whole-object colour
         "selections": [                      # optional named selections
            {"name": "feature_a", "selection": "model_a and resi 10-20",
             "color": "red"}
         ]}
      ],
      "commands": ["bg_color white", "set ray_opaque_background, 0"],  # raw PyMOL commands
      "scenes": [{"name": "01_overview_all_models", "commands": ["zoom"]}],
      "save_pse": true,                        # default true -> out_dir/<name>.pse
      "render_png": {"path": "render.png", "width": 1600, "height": 1200,
                     "dpi": 150, "ray": true}
    }

The result is always ``{"pml_path", "pse_path"?, "rendered": bool, "validation":
{"status": "PASS"|"FAIL"|"SKIPPED", "checks": [...]}, "warnings": [...],
"pymol": {"path"?, "version"?, "argv"?}}``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import DataError, InvalidInputError, ToolMissingError
from ..util import (
    atomic_write_text,
    ensure_dir,
    json_safe,
    now_iso,
    sha256_file,
    slugify,
)

#: Environment variable that may point at a PyMOL executable.
PYMOL_ENV_VAR = "LIFESCI_PYMOL"

#: Control characters (including newlines) are never allowed in PyMOL metadata:
#: the .pml format is line-oriented, so an embedded newline in a quoted name,
#: selection or comment would inject an executable command line.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_COLOR_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
#: Keys under which raw PyMOL commands are accepted (explicit trusted-code input).
_TRUSTED_COMMAND_KEYS = frozenset({"commands"})


def _safe_text(value: Any, *, label: str) -> str:
    """Return a control-character-free single-line string or raise.

    PyMOL scripts are executed by PyMOL, so metadata that may originate from
    retrieved content must never be able to break out of its quoted string or
    comment into a new command line.
    """
    text = str(value)
    if _CONTROL_RE.search(text):
        raise InvalidInputError(
            f"{label} contains control characters (including newlines), which are not allowed "
            "in PyMOL metadata because they could inject script commands",
            details={"label": label, "value": json_safe(text)},
        )
    return text


def _safe_color(value: Any, *, label: str) -> str:
    """Validate a PyMOL colour/atom-selection style token (no script syntax)."""
    text = _safe_text(value, label=label)
    if not _COLOR_RE.fullmatch(text):
        raise InvalidInputError(
            f"{label} must match {_COLOR_RE.pattern!r} (a PyMOL colour name), got {text!r}",
            details={"label": label, "value": text},
        )
    return text
#: Default timeout (s) for a headless PyMOL run.
DEFAULT_PYMOL_TIMEOUT = 300.0


# ---------------------------------------------------------------------------
# executable discovery
# ---------------------------------------------------------------------------


def find_pymol(explicit: str | None = None) -> str | None:
    """Locate a PyMOL executable: explicit path, ``$LIFESCI_PYMOL`` or ``PATH``."""
    for candidate in (explicit, os.environ.get(PYMOL_ENV_VAR), "pymol"):
        if not candidate:
            continue
        resolved = shutil.which(candidate) if os.path.sep not in candidate else (
            candidate if Path(candidate).is_file() else None
        )
        if resolved:
            return str(resolved)
    return None


def pymol_version(executable: str | None = None, *, timeout: float = 30.0) -> str | None:
    """Return the PyMOL version string if the executable can be queried."""
    path = find_pymol(executable)
    if not path:
        return None
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=float(timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - env dependent
        return None
    text = (completed.stdout or "") + (completed.stderr or "")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("pymol"):
            return stripped
    return None


# ---------------------------------------------------------------------------
# .pml generation
# ---------------------------------------------------------------------------


def _quote(value: Any) -> str:
    """Quote a metadata string for a PyMOL command (sanditised, escaped)."""
    text = _safe_text(value, label="PyMOL string").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _normalise_objects(spec: Mapping[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    raw_objects = spec.get("objects") or []
    if not isinstance(raw_objects, Sequence) or isinstance(raw_objects, (str, bytes)):
        raise InvalidInputError("pymol session spec 'objects' must be a list")
    objects: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_objects):
        if isinstance(raw, (str, os.PathLike)):
            raw = {"path": str(raw)}
        if not isinstance(raw, Mapping) or not raw.get("path"):
            raise InvalidInputError(
                f"pymol object {index} needs a 'path'",
                details={"object": json_safe(raw)},
            )
        source = Path(str(raw["path"]))
        if not source.is_absolute() and not source.is_file():
            relative = (out_dir / source)
            source = relative if relative.is_file() else source
        if not source.is_file():
            raise DataError(
                f"pymol object file not found: {source}",
                details={"path": str(source)},
            )
        name = _safe_text(
            raw.get("name") or slugify(source.stem, fallback=f"object_{index}"),
            label=f"pymol object {index} name",
        )
        load_type = None if raw.get("load_type") is None else _safe_text(
            raw["load_type"], label=f"pymol object {name!r} load_type"
        )
        color = None if raw.get("color") is None else _safe_color(
            raw["color"], label=f"pymol object {name!r} color"
        )
        spec_selections = []
        for selection in raw.get("selections") or []:
            if not isinstance(selection, Mapping) or not selection.get("name"):
                raise InvalidInputError(
                    f"pymol selection in object {name!r} needs 'name' and 'selection'",
                    details={"selection": json_safe(selection)},
                )
            spec_selections.append(
                {
                    "name": _safe_text(selection["name"], label="pymol selection name"),
                    "selection": _safe_text(
                        selection.get("selection") or "all",
                        label=f"pymol selection {selection['name']!r} expression",
                    ),
                    "color": None
                    if selection.get("color") is None
                    else _safe_color(selection["color"], label="pymol selection color"),
                }
            )
        objects.append(
            {
                "path": source,
                "name": name,
                "load_type": load_type,
                "color": color,
                "selections": spec_selections,
            }
        )
    return objects


def _render_script(
    *,
    spec: Mapping[str, Any],
    objects: Sequence[Mapping[str, Any]],
    scenes: Sequence[Mapping[str, Any]],
    commands: Sequence[str],
    pse_path: Path | None,
    render: Mapping[str, Any] | None,
    png_path: Path | None,
) -> str:
    lines: list[str] = [
        f"# lifesci PyMOL session: {_safe_text(spec.get('name') or 'session', label='pymol session name')}",
        "# Generated deterministically by lifesci.lib.pymol_session.build_session",
        "reinitialize",
    ]
    for obj in objects:
        load_args = [_quote(str(obj["path"])), _quote(obj["name"])]
        if obj.get("load_type"):
            load_args.append(_quote(obj["load_type"]))
        lines.append("load " + ", ".join(load_args))
        if obj.get("color"):
            lines.append(f"color {_safe_color(obj['color'], label='pymol object color')}, {_quote(obj['name'])}")
        for selection in obj.get("selections") or []:
            lines.append(
                f"select {_quote(selection['name'])}, {_quote(selection['selection'])}"
            )
            if selection.get("color"):
                lines.append(
                    f"color {_safe_color(selection['color'], label='pymol selection color')}, "
                    f"{_quote(selection['name'])}"
                )
    for command in commands:
        lines.append(str(command))
    for scene in scenes:
        name = _quote(scene["name"])
        for command in scene.get("commands") or []:
            lines.append(str(command))
        lines.append(f"scene {name}, store")
    if pse_path is not None:
        # PyMOL 3.x rejects a *quoted* filename for `save` ("File format not
        # supported for export"); the subprocess runs with cwd=out_dir, so a
        # plain relative filename is both safe and correct.
        lines.append(f"save {pse_path.name}")
    if png_path is not None and render is not None:
        width = int(render.get("width", 1600))
        height = int(render.get("height", 1200))
        dpi = int(render.get("dpi", 150))
        lines.append("set ray_opaque_background, 1")
        lines.append(f"viewport {width}, {height}")
        # Same quoting quirk as `save`: pass a bare relative filename.
        lines.append(
            f"png {png_path.name}, width={width}, height={height}, dpi={dpi}"
        )
    lines.append("quit")
    return "\n".join(lines) + "\n"


def _normalise_scenes(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    scenes: list[dict[str, Any]] = []
    for raw in spec.get("scenes") or []:
        if isinstance(raw, str):
            scenes.append({"name": _safe_text(raw, label="pymol scene name"), "commands": []})
            continue
        if not isinstance(raw, Mapping) or not raw.get("name"):
            raise InvalidInputError(
                "pymol scenes need a 'name'", details={"scene": json_safe(raw)}
            )
        scenes.append(
            {
                "name": _safe_text(raw["name"], label="pymol scene name"),
                "commands": [str(command) for command in raw.get("commands") or []],
            }
        )
    return scenes


# ---------------------------------------------------------------------------
# build + validation
# ---------------------------------------------------------------------------


def _check(name: str, ok: bool, detail: str | None = None) -> dict[str, Any]:
    check: dict[str, Any] = {"name": name, "ok": bool(ok)}
    if detail:
        check["detail"] = detail
    return check


def build_session(
    ctx: Any,
    *,
    spec: Mapping[str, Any],
    out_dir: str | Path,
) -> dict[str, Any]:
    """Build a PyMOL ``.pml`` session script and run it headless when possible.

    See the module docstring for the supported ``spec``.  PyMOL is looked up via
    an explicit ``spec["pymol_path"]``, ``$LIFESCI_PYMOL`` and ``PATH``; when it
    is absent the ``.pml`` is still written and validation is reported as
    ``SKIPPED`` with a ``PYMOL_MISSING`` warning.

    Returns
    -------
    dict
        ``{"pml_path", "pse_path"?, "rendered", "validation": {"status",
        "checks"}, "warnings", "pymol": {...}}``.
    """
    if not isinstance(spec, Mapping):
        raise InvalidInputError(
            "pymol session spec must be a mapping (see module docstring)"
        )
    target_dir = ensure_dir(out_dir)
    name = slugify(str(spec.get("name") or "session"), fallback="session")
    pml_path = target_dir / f"{name}.pml"
    save_pse = bool(spec.get("save_pse", True))
    pse_path = target_dir / f"{name}.pse" if save_pse else None
    render_spec = spec.get("render_png")
    if render_spec is not None and not isinstance(render_spec, Mapping):
        render_spec = {"path": str(render_spec)}
    png_path: Path | None = None
    if isinstance(render_spec, Mapping):
        raw_png_name = Path(str(render_spec.get("path") or f"{name}.png"))
        suffix = raw_png_name.suffix or ".png"
        png_name = slugify(raw_png_name.stem, fallback=name) + suffix
        render_spec = dict(render_spec)
        render_spec["path"] = png_name
        png_path = target_dir / png_name

    objects = _normalise_objects(spec, target_dir)
    if not objects:
        raise InvalidInputError("pymol session needs at least one object to load")
    scenes = _normalise_scenes(spec)
    commands = [str(command) for command in spec.get("commands") or []]
    script = _render_script(
        spec=spec,
        objects=objects,
        scenes=scenes,
        commands=commands,
        pse_path=pse_path,
        render=render_spec if isinstance(render_spec, Mapping) else None,
        png_path=png_path,
    )
    atomic_write_text(pml_path, script)

    warnings: list[dict[str, Any]] = []
    if png_path is not None:
        warnings.append(
            {
                "code": "PNG_RENDER_LIMITATION",
                "severity": "warning",
                "message": (
                    "Headless full-session PNG rendering can silently produce an "
                    "empty file for very large sessions (observed at 183 MB / "
                    "~403k atoms); render small extracted structures instead and "
                    "verify the PNG exists and is non-empty."
                ),
            }
        )

    checks: list[dict[str, Any]] = [
        _check("pml_written", pml_path.is_file(), str(pml_path)),
        _check("objects_declared", len(objects) > 0, f"{len(objects)} object(s)"),
    ]
    executable = find_pymol(spec.get("pymol_path"))
    if executable is None:
        if ctx is not None and hasattr(ctx, "warn"):
            ctx.warn(
                "PYMOL_MISSING",
                (
                    "PyMOL executable was not found (checked spec.pymol_path, "
                    f"${PYMOL_ENV_VAR} and PATH); the .pml session was written but "
                    "rendering/validation was SKIPPED — do not report a rendered "
                    "session."
                ),
                context={"pml_path": str(pml_path)},
            )
        warnings.append(
            {
                "code": "PYMOL_MISSING",
                "severity": "warning",
                "message": "PyMOL not installed; session .pml written, validation skipped.",
            }
        )
        return {
            "pml_path": str(pml_path),
            "pse_path": None,
            "rendered": False,
            "validation": {
                "status": "SKIPPED",
                "checks": checks
                + [_check("pymol_executable", False, "not found on PATH / LIFESCI_PYMOL")],
            },
            "warnings": warnings,
            "pymol": {"path": None, "version": None, "argv": None},
        }

    version = pymol_version(executable, timeout=float(spec.get("version_timeout", 30.0)))
    argv = [executable, "-cq", str(pml_path)]
    timeout = float(
        spec.get("timeout")
        or (getattr(ctx, "timeout", None) if ctx is not None else None)
        or DEFAULT_PYMOL_TIMEOUT
    )
    if timeout < 60.0:
        timeout = DEFAULT_PYMOL_TIMEOUT
    try:
        completed = subprocess.run(
            argv,
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        exit_code = int(completed.returncode)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as err:  # pragma: no cover
        if ctx is not None and hasattr(ctx, "warn"):
            ctx.warn(
                "PYMOL_RUN_FAILED",
                f"PyMOL session run failed to execute: {err}",
                context={"argv": argv},
            )
        return {
            "pml_path": str(pml_path),
            "pse_path": None,
            "rendered": False,
            "validation": {
                "status": "FAIL",
                "checks": checks
                + [_check("pymol_executable", True, executable), _check("pymol_run", False, str(err))],
            },
            "warnings": warnings,
            "pymol": {"path": executable, "version": version, "argv": argv},
        }
    if ctx is not None and hasattr(ctx, "provenance"):
        try:
            ctx.provenance.append(
                {
                    "kind": "tool",
                    "name": "pymol",
                    "version": version,
                    "argv": argv,
                    "exit_code": exit_code,
                    "pml_sha256": sha256_file(pml_path),
                    "time": now_iso(),
                }
            )
        except OSError:  # pragma: no cover
            pass
    checks.append(_check("pymol_executable", True, executable))
    checks.append(
        _check(
            "pymol_run",
            exit_code == 0,
            f"exit_code={exit_code}" + (f"; stderr={stderr.strip()[:400]}" if exit_code else ""),
        )
    )
    pse_ok = True
    if pse_path is not None:
        pse_ok = pse_path.is_file() and pse_path.stat().st_size > 0
        checks.append(
            _check(
                "pse_written",
                pse_ok,
                str(pse_path) if pse_ok else f"missing or empty: {pse_path}",
            )
        )
    rendered = False
    if png_path is not None:
        png_ok = png_path.is_file() and png_path.stat().st_size > 0
        rendered = bool(png_ok)
        checks.append(
            _check(
                "png_written",
                png_ok,
                str(png_path) if png_ok else f"missing or empty: {png_path}",
            )
        )
        if not png_ok and ctx is not None and hasattr(ctx, "warn"):
            ctx.warn(
                "PNG_RENDER_LIMITATION",
                (
                    "PyMOL ran but the requested PNG was not produced or is empty "
                    "(known headless full-session limitation); rendering from a small "
                    "extracted structure is more reliable."
                ),
                context={"png_path": str(png_path)},
            )
    failed = [check for check in checks if not check["ok"]]
    status = "FAIL" if failed else "PASS"
    if status == "FAIL" and ctx is not None and hasattr(ctx, "warn"):
        ctx.warn(
            "PYMOL_VALIDATION_FAILED",
            (
                "PyMOL session validation FAILED: "
                + ", ".join(check["name"] for check in failed)
            ),
            context={"checks": failed},
        )
    return {
        "pml_path": str(pml_path),
        "pse_path": str(pse_path) if pse_path is not None and pse_path.is_file() else None,
        "rendered": rendered,
        "validation": {"status": status, "checks": checks},
        "warnings": warnings,
        "pymol": {
            "path": executable,
            "version": version,
            "argv": argv,
            "exit_code": exit_code,
            "stdout_tail": stdout.strip().splitlines()[-5:] if stdout.strip() else [],
            "stderr_tail": stderr.strip().splitlines()[-5:] if stderr.strip() else [],
        },
    }


def require_pymol(explicit: str | None = None) -> str:
    """Return the PyMOL executable path or raise :class:`ToolMissingError`."""
    path = find_pymol(explicit)
    if path is None:
        raise ToolMissingError(
            "PyMOL is required for this operation but was not found",
            details={"env_var": PYMOL_ENV_VAR},
            fallbacks=[
                "install PyMOL (conda install -c conda-forge pymol-open-source)",
                f"set {PYMOL_ENV_VAR} to the pymol executable",
                "the .pml session script can still be generated without PyMOL",
            ],
        )
    return path
