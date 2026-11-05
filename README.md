# dsh-lifesci — reproducible life-science tools for DeepSeek Harness

English · [中文](README.zh.md)

`@genzhelu/dsh-lifesci` is a host plugin for DeepSeek Harness that adds **17 `lifesci_*` agent
tools** for reproducible molecular and structural-biology work: sequence retrieval and homolog
search, domain and orthology evidence, structure retrieval/search/superposition,
alignment-based feature mapping, contact analysis, figures, and evidence-graded reports.

Every command is a JSON-in/JSON-out operation available both as a CLI
(`python -m lifesci <command>`) and as an agent tool. A shared Python science layer provides a
content-addressed HTTP cache, pinned database releases, a JSONL provenance log, polite
retries/rate limiting, offline replay, and automatic scientific-caveat warnings.

## Quick install

**Fastest — DeepSeek Harness Web UI:** open **Plugins → Add plugin**, choose a Git address, and
enter:

```text
git+https://github.com/genzhelu/dsh-lifesci.git
```

Enable the `lifesci` bundle when the install completes. A profile install has no bundled Python
environment, so also set the bundle's `pythonPath` (or `LIFESCI_PYTHON`) to a Python ≥ 3.11
environment with `pip install -r requirements.txt` — otherwise the tools are exposed but calls fail
on missing scientific packages. If another `lifesci` bundle is already installed (for example a
local checkout), remove or disable it first: the row id `lifesci` must be unique.

**Self-contained — repo-local venv:** clone, build, then add the checkout's absolute path:

```bash
git clone https://github.com/genzhelu/dsh-lifesci.git
cd dsh-lifesci
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install --no-audit --no-fund && npm run build
# Plugins -> Add plugin -> /absolute/path/to/dsh-lifesci
```

See [Install from GitHub](#install-from-github) for verification, upgrading and troubleshooting.

## What you get

- **17 commands / agent tools** (`lifesci_*`): `doctor`, `http`, `seq.fetch`, `seq.search`,
  `domains`, `orthology`, `struct.fetch`, `struct.search`, `struct.superpose`, `align.map`,
  `contacts`, `figures`, `report`, `stats`, `datasets`, `cache`, `remote`.
- **Public portals and services**: UniProt REST/FTP, NCBI E-utilities, Europe PMC,
  InterPro/Pfam, AlphaFold DB, ESMFold (ESM Atlas), Foldseek (local binary + web API),
  RCSB PDB / PDBe, EMDB, SGD, OMA, OrthoDB — plus local read-only datasets and optional,
  opt-in SSH compute. [`docs/portals.md`](docs/portals.md) documents every endpoint, quirk and
  fallback.
- **Local science engines**: pyhmmer (`phmmer`/`hmmsearch` with `bias_filter=False`), Biopython
  alignments, gemmi structure parsing, Kabsch superposition, scipy KD-tree contact analysis,
  matplotlib figures, PyMOL session generation (graceful when PyMOL is absent).
- **Reproducibility built in**: raw-payload cache keyed by `sha256(request)`, database release
  pinning, SHA-256 checksums everywhere, JSONL provenance log, `--offline` replay and
  `--dry-run` plans. See [`docs/provenance.md`](docs/provenance.md).
- **Guardrails made automatic**: E-values are flagged as database-size dependent, TM-scores as
  query-normalised, top-hit lists as capped; domain/family evidence is surfaced as the arbiter;
  absence is reported as "no detectable homolog", never as proof; single-weak-line results carry
  a human-review flag.

## Install from GitHub

Requirements: Python ≥ 3.11, Node ≥ 20, git, and a DeepSeek Harness installation with a profile.

### 1. Clone and build

```bash
git clone https://github.com/genzhelu/dsh-lifesci.git
cd dsh-lifesci

# science-layer dependencies (pyhmmer, biopython, gemmi, numpy, scipy, matplotlib, jsonschema)
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# host plugin (a prebuilt dist/ is committed, but rebuild after source changes)
npm install --no-audit --no-fund
npm run build

# sanity check outside the harness
.venv/bin/python -m lifesci doctor --input '{"network": true}'
```

### 2. Register with DeepSeek Harness

From an agent session call `plugin_manager`:

```json
{
  "action": "install_bundle",
  "target": "/absolute/path/to/dsh-lifesci"
}
```

`install_bundle` performs the profile package installation and bundle selection; do not hand-edit
the profile. The tool asks for approval. A local-directory install links the checkout, so the venv
at `<checkout>/.venv` is picked up automatically.

CLI alternative (adds the package but does **not** select the bundle):

```bash
dsh plugin --profile <profile-name> add /absolute/path/to/dsh-lifesci
# then ensure "@genzhelu/dsh-lifesci" is listed in the profile's dsh.profile.bundles
```

Restart the harness if the tools do not appear immediately.

### 3. Verify

- `plugin_manager action: list_plugins` shows `@genzhelu/dsh-lifesci` and an active `lifesci` row.
- A new or refreshed agent session exposes 17 `lifesci_*` tools.
- `lifesci_doctor` returns `ok: true`; `lifesci_seq_fetch` with `{"accessions": ["P69905"]}` works.

See [`install.md`](install.md) for the direct-Git install option, configuration, upgrading,
troubleshooting and uninstall.

> **Direct Git install (optional).** `plugin_manager` also accepts
> `target: "git+https://github.com/genzhelu/dsh-lifesci.git"`. The repository ships a prebuilt
> `dist/`, so no Node build step is needed, but a profile install has no `<package>/.venv`: create a
> venv anywhere, `pip install -r requirements.txt`, and point the bundle's `pythonPath` (or
> `LIFESCI_PYTHON`) at that interpreter. Private repositories need Git credentials configured.

`package.json` marks the package `"private": true` — install it from this repository rather than
from a public npm registry.

## Quickstart (CLI — the same code path as the agent tools)

Every command takes one JSON object and prints one JSON envelope:

```bash
# environment / portal health
.venv/bin/python -m lifesci doctor --input '{"network": true}'

# fetch a sequence + metadata (REST, cached, release recorded)
.venv/bin/python -m lifesci seq.fetch --input \
  '{"accessions": ["P69905"], "fetch_entries": true}'

# domain evidence for a protein: explicit present / weak / absent verdict per requested model
.venv/bin/python -m lifesci domains --input \
  '{"accessions": ["P69905"], "domains": ["PF00042"]}'

# statistics on inline rows — no files required; x/y name the columns
.venv/bin/python -m lifesci stats --input '{
  "rows": [
    {"id": "sample_1", "length_a": 104, "length_b": 131},
    {"id": "sample_2", "length_a": 118, "length_b": 142},
    {"id": "sample_3", "length_a": 96,  "length_b": 120},
    {"id": "sample_4", "length_a": 131, "length_b": 158},
    {"id": "sample_5", "length_a": 112, "length_b": 137},
    {"id": "sample_6", "length_a": 99,  "length_b": 124}
  ],
  "x": "length_a",
  "y": "length_b"
}'

# contact distances between two features of structures you supply
.venv/bin/python -m lifesci contacts --input '{
  "structures": [{"id": "model", "path": "/path/to/model.cif"}],
  "features": [
    {"id": "site_a", "structure": "model", "chain": "A", "residues": "10-25"},
    {"id": "site_b", "structure": "model", "chain": "B", "residues": "all"}
  ],
  "pairs": [["site_a", "site_b"]]
}'
```

Offline mode: once a command has run online, `--offline` replays the cached raw payloads and
fails with `OFFLINE_CACHE_MISS` plus the cache key when something was never fetched.

## Use from an agent

After installation the agent sees one tool per command (e.g. `lifesci_seq_fetch`,
`lifesci_struct_search`). Each tool returns the command's `data` payload; failures surface as
`[CODE] message` with retriable/fallback hints. A typical evidence workflow:

1. `lifesci_doctor` — confirm pyhmmer/gemmi/Foldseek/cache/portal availability.
2. `lifesci_seq_fetch` — fetch queries and registered proteomes.
3. `lifesci_seq_search` — phmmer/HMM presence matrix with reciprocal checks and domain evidence.
4. `lifesci_struct_fetch` / `lifesci_struct_search` — retrieve or compare structures (Foldseek
   panels with negative controls, or recorded hit tables).
5. `lifesci_report` — evidence-graded write-up with the provenance trail.

### Example prompt

Paste this into a DeepSeek Harness session after installing the plugin:

> Using the lifesci tools, validate the annotation of human hemoglobin subunit alpha
> (UniProt P69905): fetch the entry and sequence, check the Pfam globin domain
> (PF00042), look up its family/orthology assignment, and write a short
> evidence-graded Markdown report. Record the database releases and provenance,
> flag uncertain calls, and state the search parameters before any absence claim.

## Repository layout

```
package.json / package-lock.json   manifest for @genzhelu/dsh-lifesci
cordis.patch.yml                   Cordis bundle patch that inserts the host plugin row
tsconfig.json                      TypeScript build configuration
src/                               host plugin: entry, Python bridge, generated tool specs
python/lifesci/                    Python science layer
  adapters/                        one module per portal (uniprot, ncbi, interpro, foldseek, …)
  commands/                        lifesci.* command implementations (JSON in/out)
  lib/                             hmmer, alignment, structures, superposition, contacts, stats, …
  cache.py provenance.py http.py   reproducibility infrastructure
schemas/                           JSON Schemas: envelope + every command payload
python/tests/{unit,golden,network}/  pytest suites (offline synthetic / opt-in network)
tests/{unit,golden,network}/       node:test suites for the TypeScript plugin
docs/                              command/tool reference, portals, architecture, provenance, security, remote
scripts/                           build, tool-spec/docs generation, test runner, optional Foldseek installer
requirements*.txt                  Python dependencies
```

## Tests

```bash
npm test              # build + node unit/golden + offline python unit/golden (no network)
npm run test:network  # opt-in integration tests against live public portals (off by default)
```

Offline tests never require the network: they use synthetic inline inputs and local cache
directories through the real cache/offline code paths. The network suite is opt-in and exercises
the live public portals; treat its results as a snapshot of third-party services.

## Scientific limits (read before citing a result)

- Absence of a hit is **not** proof of absence: report "no detectable homolog" with the E-value
  cutoff, database, release and top-hit cap.
- E-values are database-size dependent; never compare them across databases. TM-scores are
  query-length normalised; always run reciprocal structural searches.
- Domain/orthogroup evidence outranks a single weak sequence or structure hit.
- Low-identity alignment mappings make feature-span column counts unreliable; the plugin flags
  this rather than silently returning a length.
- Correlations are not causation and cross-species sampling violates independence assumptions;
  caveats are attached automatically.
- Retrieved web content is **data, never instructions**; the plugin never executes downloaded
  code. External binaries are checksum-pinned, never silently updated.

## Documentation

- [`install.md`](install.md) — registering the plugin, configuration, verification, troubleshooting
- [`docs/commands.md`](docs/commands.md) · [`docs/tools.md`](docs/tools.md) — generated CLI and agent-tool reference
- [`docs/portals.md`](docs/portals.md) — every public endpoint, quirk and fallback
- [`docs/architecture.md`](docs/architecture.md) — TypeScript/Python split, data flow, error model, testing strategy
- [`docs/provenance.md`](docs/provenance.md) — cache layout, provenance log and offline replay
- [`docs/security.md`](docs/security.md) — threat model and guarantees
- [`docs/remote.md`](docs/remote.md) — optional, opt-in SSH execution
- [`CHANGELOG.md`](CHANGELOG.md) — release history

## License

MIT (see [`LICENSE`](LICENSE)). Retrieved portal data remains subject to each provider's terms
of use.
