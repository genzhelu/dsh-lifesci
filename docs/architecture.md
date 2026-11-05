# Architecture

`dsh-lifesci` is split into a thin **TypeScript host plugin** (the part the DeepSeek Harness
loads and the agent talks to) and a substantial **Python science layer** (where the science and
reproducibility machinery lives). The split keeps the host plugin zero-dependency and stable
against harness internals, while the science layer can be developed, tested, and reused from the
command line independently.

```
                   DeepSeek Harness (host process)
┌────────────────────────────────────────────────────────────────────────────┐
│  Cordis Loader ── mounts bundle patch row: id=lifesci                      │
│  src/index.ts  (apply(ctx, config))                                        │
│    └─ for each generated tool spec: ctx.tools.register({ name, parameters,  │
│       execute, output, presentCall })                                       │
│         execute(args, exec) ──► src/bridge.ts                               │
│              spawn: <python> -m lifesci <command> --input <json>   (child    │
│              PYTHONPATH=<repo>/python, cwd=session workspace)     process)   │
└──────────────────────────────────────┬─────────────────────────────────────┘
                                       │ one JSON envelope on stdout
                                       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  Python science layer (python/lifesci)                                      │
│                                                                            │
│  cli.py          JSON in → validate → run command → JSON envelope out       │
│  commands/*.py   one Command per operation (registry = single source of     │
│                  truth for CLI + model-facing tool specs)                   │
│  adapters/*.py   one module per portal; parse raw payloads into data        │
│  lib/*.py        hmmer, alignment, structures, superposition, contacts,     │
│                  stats, evidence curation, figures, PyMOL, reporting        │
│  http.py         retries + backoff + Retry-After + per-host token buckets   │
│  cache.py        content-addressed payload cache (sha256(request)) + TTL    │
│  provenance.py   JSONL request log, SHA-256 manifests, release pinning      │
│  envelope.py     envelope construction + JSON-schema self-validation        │
└────────────────────────────────────────────────────────────────────────────┘
```

## Why a Python bridge instead of reimplementing in TypeScript

The scientific stack (pyhmmer, Biopython, gemmi, NumPy/SciPy, matplotlib) is Python-only and
mature; a TypeScript reimplementation would be a reliability downgrade. The bridge is a single
`spawn` per tool call: no daemon, no hidden global state, no long-lived RPC channel that can
silently drift. JSON-in/JSON-out makes the boundary explicit and testable, and the same code
path serves the CLI (`python -m lifesci …`) and the agent tools.

## Data flow of one command

1. `bridge.ts` builds `argv` from tool args, sets `PYTHONPATH`, and picks the cwd from the
   session workspace (so relative paths and the default cache land inside the writable project).
2. `cli.py` parses the JSON input, validates it against the command's JSON Schema, creates a
   `Context` (cache dir, log path, offline/dry-run flags, retry policy, transport), and runs the
   handler.
3. The handler calls adapters (through `http.py`) and/or the science library; every fetch is
   cached and provenance-logged; every scientific caveat is appended as a machine-readable
   warning; weak single-line results are flagged for human review.
4. `cli.py` validates the payload against `schemas/<command>.schema.json`, wraps it with
   provenance/warnings into the envelope, self-validates the envelope, writes the provenance log,
   and prints exactly one JSON object.
5. `bridge.ts` parses the envelope: `ok:true` → returns `data` to the model (rendered as pretty
   JSON); `ok:false` → throws `[CODE] message` so the model can branch on stable error codes.

## Reproducibility model

- **Cache key** = SHA-256 of canonical JSON `{format, method, url, params, relevant headers,
  body hash}`. Bodies are stored verbatim next to a metadata sidecar containing status, headers,
  body SHA-256, byte count, retrieval time, release, and database.
- **Offline replay** reads only the cache; a miss is a typed `OFFLINE_CACHE_MISS` naming the
  cache key. Offline tests exercise the cache code path rather than bypassing it.
- **Release pinning** — adapters record observed database releases (`x-uniprot-release`,
  InterPro/Foldseek versions) to `releases.json`; reports and `doctor` surface them.
- **Provenance** — every HTTP request, local file read, external tool run, and command summary is
  appended to a JSONL log and returned in the envelope. Credential-like values are redacted;
  request bodies are logged by SHA-256 only.
- **Determinism** — commands sort unordered lists and avoid time/randomness in `data`; figures
  record their SHA-256.

## Error model

`lifesci.errors.LifesciError` subclasses carry `code`, `message`, `retriable`, `details`,
`fallbacks`. The CLI maps them to exit codes (0 ok, 2 usage, 3 not found, 4 offline/cache,
5 portal/data, 6 missing tool, 7 internal, 8 remote) and to the envelope error object. The agent
sees `[CODE] message` plus retriable/fallback hints; portal errors add the attempt diagnostics
and a redacted response preview.

## Deliberate non-goals

No BLAST-queue wrapper, no long-running structure prediction, no database writes, no silent
auto-updates, no automated biological conclusions. This plugin is a **tool layer with
provenance**; scientific judgement stays with the agent and human reviewer.

## Testing strategy

- `python/tests/unit` — parsers, cache, rate limiter, HTTP retry/offline behaviour with an
  injected `FakeTransport`, evidence curation, geometry/statistics math, CLI/envelope contract.
- `python/tests/golden` — offline end-to-end checks of generic workflows (statistics, reporting,
  alignment mapping, contacts, sequence-search evidence, figures) using synthetic inline inputs
  and a seeded cache; no network.
- `python/tests/network` — opt-in integration checks against real public portals
  (`LIFESCI_NETWORK=1`); skipped by default.
- `tests/unit|golden` (Node) — TypeScript host plugin: tool-spec shape, bridge argument/error
  construction, and a real offline CLI round-trip through the bridge.
