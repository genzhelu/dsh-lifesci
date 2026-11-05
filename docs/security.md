# Security model

The plugin handles untrusted web payloads, runs local scientific binaries, optionally uses SSH,
and writes caches in the user's project. This document states the guarantees and their limits.

## 1. Retrieved content is data, never instructions

- Portal responses (JSON, XML, FASTA, HTML error pages) are parsed as data only. The science
  layer never executes code, templates, YAML anchors, shell fragments, or `eval` from downloaded
  payloads. FASTA/PDB/mmCIF content is parsed by Biopython/gemmi/pyhmmer parsers or strict
  first-party parsers.
- HTML error pages from flaky portals (e.g. NCBI `eutils201 WWW Error 500`) are treated as error
  text; only a truncated, whitespace-collapsed preview enters diagnostics.
- Agent-facing tool output is JSON `data` plus text warnings. The plugin never forwards raw page
  text as if it were an instruction. `web`-content caveats that matter scientifically (e.g. a
  paper's claim) must be graded by the agent — see `lifesci_report`, which requires an
  ESTABLISHED/INFERRED/UNKNOWN grade for every claim.

## 2. Credentials and configuration

- No credentials are stored in the repository, cache, logs, or tool outputs.
- Optional services that need keys (BioGRID etc.) are **not** required and are not implemented as
  key-backed defaults. Remote SSH hosts are configured through the user's environment
  (`LIFESCI_REMOTE_HOSTS` / `LIFESCI_REMOTE_CONFIG`), never committed.
- Provenance redacts keys whose names contain `token`, `secret`, `password`, `api_key`,
  `authorization`, `cookie`. Request bodies are recorded by SHA-256 only.
- HTTP request headers include only a descriptive `User-Agent` and optional contact `From`
  (`LIFESCI_CONTACT_EMAIL`), so portal operators can identify the tool's traffic.

## 3. Writes are confined

- The science layer writes only to: the content-addressed cache directory (`--cache-dir`,
  default `<cwd>/.lifesci-cache`), the provenance log (inside the cache dir or `--log`), explicit
  `out`/`out_dir` paths supplied by the caller, and system temp directories for scratch work.
- No writes outside those paths; no hidden global state; no daemon. `python -m lifesci` is a
  one-shot process per call.
- The cache is content-addressed: a write can only create/refresh the entry for the exact
  request key; it never overwrites arbitrary user files.
- `cache clear` requires explicit `confirm: true`; `datasets register` is read-only (it records a
  checksum of existing files and refuses to modify them).

## 4. Downloaded binaries

- Foldseek is the only downloaded executable. `scripts/install_foldseek.sh` pins the release and
  verifies the archive SHA-256 before extracting; `doctor` reports version + binary SHA-256.
- There is no silent auto-update. A version mismatch is reported, not "fixed" automatically.
- External tools run in a scratch directory with arguments built as arrays (no shell string
  interpolation), timeouts enforced, output captured, and provenance recorded (argv, version,
  start/end, exit code).

## 5. Remote execution is opt-in and off by default

See [`remote.md`](remote.md). Remote commands are disabled unless hosts are explicitly configured
in the user's environment **and** the caller passes the enable/allow flag. The command builds
`ssh` argument arrays (no shell expansion of host/user), never prints key paths/content beyond the
host alias, and records host + argv + timestamps in provenance.

## 6. Network surface

The plugin contacts only the hosts documented in [`portals.md`](portals.md) (UniProt, EBI with
Europe PMC and InterPro, NCBI, AlphaFold DB, ESM Atlas, Foldseek web, RCSB, SGD, OMA, OrthoDB).
Requests are polite: descriptive User-Agent, per-host token buckets, exponential backoff,
`Retry-After` honouring, and host-wide cooldowns after 429/503. HTTP redirects are followed by the
standard library; redirect targets are not restricted, so payloads from an unexpectedly
redirected host should be treated like any other untrusted data.

## 7. Dependencies and supply chain

- Runtime Python dependencies are pinned in `requirements.txt` (pyhmmer pinned exactly);
  the TypeScript host plugin has **zero runtime dependencies** and all dev dependencies are
  declared in `package.json` for building only.
- The bundle patch contains only a single `insert` row; it does not alter other harness rows.
- Installation is performed by the harness `plugin_manager` (approved tool call), not by running
  arbitrary shell commands from the plugin.

## 8. Known limitations

- The cache and provenance log contain public scientific data and request metadata; they are not
  encrypted. Do not point `--log` at locations where that metadata is sensitive.
- A malicious or compromised portal could return malformed data that passes parsers in ways that
  produce wrong numbers. The plugin cannot validate upstream science; it does record raw payload
  SHA-256s so a reviewer can audit and replay exactly what was received.
- The Python bridge runs a local interpreter with the user's permissions; it is not a sandbox.
  Run the harness under the least privilege appropriate for your workflows.
