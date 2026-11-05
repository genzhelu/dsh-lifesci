# Command reference (`lifesci.*`)

Generated from the Python command registry by `scripts/generate_docs.py`; do not edit by hand.

Every command takes one JSON object and returns one JSON envelope (`data`, `provenance[]`, `warnings[]`, `human_review[]`; see [provenance.md](provenance.md)). Agent tools have the same names with dots replaced by underscores and a `lifesci_` prefix (e.g. `lifesci_seq_fetch`).

## Index

| command | tool | category | summary |
|---|---|---|---|
| `align.map` | `lifesci_align_map` | alignment | Project query sequences onto a reference alignment row; report identity, coverage, feature lengths. |
| `cache` | `lifesci_cache` | foundation | Report cache stats, list/verify entries, prune expired payloads, or clear the cache. |
| `contacts` | `lifesci_contacts` | structure | Minimum atom-atom distances, closest atom pairs, percentiles, missing features, and bridges. |
| `datasets` | `lifesci_datasets` | foundation | Register, list, verify, resolve, or unregister checksum-pinned local reference data. |
| `doctor` | `lifesci_doctor` | foundation | Report dependency versions, cache health, tool availability, and portal reachability. |
| `domains` | `lifesci_domains` | sequence | InterPro/Pfam domain scan, recorded-evidence replay, and/or local HMM scan with explicit domain-absent verdicts. |
| `figures` | `lifesci_figures` | visualisation | Render log-log covariance / histogram / contact-bar figures with matplotlib Agg, and build PyMOL sessions. |
| `http` | `lifesci_http` | foundation | Perform one retrying/caching/rate-limited HTTP request with full provenance. |
| `orthology` | `lifesci_orthology` | sequence | Query PANTHER, OrthoDB, eggNOG and OMA and report an agree/disagree/unknown cross-check table. |
| `remote` | `lifesci_remote` | foundation | List configured SSH hosts, plan a remote job, or run it with explicit opt-in. |
| `report` | `lifesci_report` | reporting | Render graded claims (ESTABLISHED/INFERRED/UNKNOWN + PMID/DOI) with caveats and a provenance table. |
| `seq.fetch` | `lifesci_seq_fetch` | sequence | Fetch UniProtKB FASTA sequences, normalised entry metadata, and reference proteomes with release pinning. |
| `seq.search` | `lifesci_seq_search` | sequence | pyhmmer phmmer/hmmsearch or offline replay of recorded evidence, curated into a presence matrix. |
| `stats` | `lifesci_stats` | statistics | Compute Pearson/Spearman/partial correlations, a log-log fit, group trends, and outliers. |
| `struct.fetch` | `lifesci_struct_fetch` | structure | Download AlphaFold DB models (with ESMFold fallback), PDB structures and EMDB maps; record SHA-256. |
| `struct.search` | `lifesci_struct_search` | structure | Controlled local Foldseek panel with negative-control calibration, Foldseek web API, or recorded-table replay with sanity anchors. |
| `struct.superpose` | `lifesci_struct_superpose` | structure | Column-based superposition with transforms/core/local RMSD, or pairwise Foldseek TM alignment. |

## `align.map` — Map sequences onto a reference MSA row and measure feature spans

Aligns each query to a reference row of an MSA using tuned PairwiseAligner settings (match +2, mismatch -1, gap open -8, extend -0.5, cheap end gaps), records the residue→column map, and counts query residues inside feature column spans or reference-row coordinate ranges. Returns per query: length, n_aligned, identity, coverage, per-feature lengths + residues, and flags. Warns LOW_IDENTITY_MAPPING below min_identity (default 0.60) and FEATURE_SPAN_UNSTABLE because feature-span column counts are unreliable at low identity — always report identity with the lengths. reference_row='auto' tries every candidate row per query and keeps the highest-identity one (restrict with reference_rows); the chosen row is reported per query. Use feature_spans (0-based inclusive alignment columns) or feature_coords (1-based ungapped reference-row positions).

- Tool: `lifesci_align_map`
- Category: `alignment` · side effects: `read`
- Payload schema: [`schemas/align.map.schema.json`](../schemas/align.map.schema.json)
- Recommended tool timeout: 300000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `alignment` | string | yes |  | Path to the reference alignment (FASTA, Stockholm, or Clustal; gzip ok). Rows may carry '/start-end' coordinate suffixes, which are matched leniently. |
| `reference_row` | string | no |  | Name of the alignment row used as the reference coordinate system; the '/start-end' suffix may be omitted. Defaults to the first row. Use 'auto' to align each query to every candidate row and keep the row with the highest identity (ties keep the first row in file order) — the chosen row is reported per query in data.queries[].reference_row. |
| `reference_rows` | array<string> | no |  | Only with reference_row='auto': restrict the candidate reference rows to this list of names (recommended for large alignments; a warning is emitted when more than 50 candidates are tried). |
| `queries` | array<object> | no |  | Inline query sequences (alternative to query_fasta). |
| `query_fasta` | string | no |  | Path to a FASTA file of query sequences (alternative to queries). |
| `feature_spans` | object | no |  | Feature definitions as alignment-column spans (0-based inclusive), e.g. {'feature_a': [[10, 60]], 'feature_b': [[200, 260]]}. A flat [start,end] pair or a list of column indices is also accepted. |
| `feature_coords` | object | no |  | Feature definitions as 1-based ungapped reference-row coordinates, e.g. {'feature_a': [[11, 61]], 'feature_b': [[201, 261]]}; converted to column spans through the reference row. |
| `coords_reference_row` | string | no |  | Alignment row whose ungapped sequence defines feature_coords/reference coordinates (default: the reference row). Use the row the coordinates were annotated on when it differs from the mapping reference row. |
| `features_file` | string | no |  | JSON file with feature definitions. Accepts a plain {feature: span(s)} map (treated as column spans by default), or keys feature_spans/feature_coords/coordinate_system. |
| `min_identity` | number | no | `0.6` | Identity threshold below which LOW_IDENTITY_MAPPING and FEATURE_SPAN_UNSTABLE are raised. |
| `out` | string | no |  | Optional path to write the mapping payload JSON. |

Examples:

```json
{
  "alignment": "alignments/reference.fa",
  "feature_spans": {
    "feature_a": [
      [
        2,
        6
      ]
    ],
    "feature_b": [
      [
        10,
        14
      ]
    ]
  },
  "queries": [
    {
      "name": "sample_1",
      "sequence": "ACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT"
    }
  ],
  "reference_row": "reference_1"
}
```


## `cache` — Inspect and maintain the lifesci cache

Maintenance for the content-addressed HTTP cache that makes lifesci runs replayable offline. Actions: 'stats' (counts/bytes/databases/expired), 'list' (deterministic per-entry metadata), 'verify' (re-hash bodies; reports corruption), 'prune' (delete expired entries; honour dry_run), 'clear' (delete everything; requires confirm:true). This command never touches the network, so it is safe under --offline. Clearing the cache destroys the ability to reproduce prior results offline; verify first and export anything you need. Entries are sorted by cache key for deterministic output.

- Tool: `lifesci_cache`
- Category: `foundation` · side effects: `write`
- Payload schema: [`schemas/cache.schema.json`](../schemas/cache.schema.json)
- Recommended tool timeout: 300000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `action` | enum(stats\|list\|verify\|prune\|clear) | yes |  | Cache operation: 'stats' summary counts; 'list' per-entry metadata; 'verify' re-hash cached bodies; 'prune' delete expired entries; 'clear' delete the whole cache (requires confirm:true). |
| `limit` | integer | no |  | Maximum number of entries to list or verify (deterministic key order). Omit for all entries. |
| `database` | string | no |  | Only include entries recorded for this database name (e.g. 'uniprot', 'interpro'); omitted means every database. |
| `expired_only` | boolean | no | `false` | For 'list'/'verify': only include entries whose TTL has expired. |
| `dry_run` | boolean | no | `false` | For 'prune': report what would be removed without deleting anything. |
| `confirm` | boolean | no | `false` | For 'clear': must be true. This is a deliberate second confirmation because clearing destroys the offline replay cache. |

Examples:

```json
{
  "action": "stats"
}
```
```json
{
  "action": "list",
  "limit": 20
}
```
```json
{
  "action": "prune",
  "dry_run": true
}
```
```json
{
  "action": "clear",
  "confirm": true
}
```


## `contacts` — Inter-element contact distances

Measures minimum atom-atom distances between named structural features across one or more structures. RNA features may be selected by explicit residues ('101-120') or by alignment-column spans resolved through a residue→column colmap JSON; protein features use all atoms by default. Returns per pair: min_distance, closest atom/residue pair, n_under counts for thresholds (default 4/5/10 Å), and the percentile position of the contact along each element; missing features are reported explicitly, and bridges through a third feature trigger CONTACT_BRIDGE_THROUGH_THIRD_FEATURE. Always warns DISTANCE_ATOM_SELECTION (the value depends on the atom selection; P atoms ignore side chains) and CONTACT_ABSENT_NOT_PROOF (no contact in these structures/thresholds is not evidence of no interaction).

- Tool: `lifesci_contacts`
- Category: `structure` · side effects: `read`
- Payload schema: [`schemas/contacts.schema.json`](../schemas/contacts.schema.json)
- Recommended tool timeout: 600000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `structures` | array<object> | yes |  | Structures to analyse; every feature belongs to exactly one of them. |
| `features` | array<oneOf> | yes |  | Named features (rRNA elements or protein chains) to measure distances between. |
| `pairs` | array<oneOf> | no |  | Feature pairs to measure. Default: every pair of features within each structure. |
| `thresholds` | array<number> | no | `[4.0, 5.0, 10.0]` | Distance thresholds (Å) for the n_under counts. |
| `top_pairs` | integer | no | `10` | Number of closest atom pairs retained per feature pair (the minimum distance is always first). |
| `bridge_threshold` | number | no | `4.5` | Å threshold for bridge detection: warn when a third feature lies within this distance of both members of a non-contacting pair. |
| `out` | string | no |  | Optional path to write the contact payload JSON. |

Examples:

```json
{
  "features": [
    {
      "atoms": [
        "P"
      ],
      "columns": "feature_a_columns",
      "id": "feature_a",
      "structure": "structure_a"
    },
    {
      "atoms": [
        "P"
      ],
      "columns": "feature_b_columns",
      "id": "feature_b",
      "structure": "structure_a"
    },
    {
      "chain": "A",
      "id": "chain_x",
      "residues": "all",
      "structure": "structure_a"
    }
  ],
  "structures": [
    {
      "colmap": "structures/structure_a_colmap.json",
      "id": "structure_a",
      "path": "structures/structure_a.cif"
    }
  ]
}
```


## `datasets` — Manage local read-only datasets

Local reference data (alignments, ES-length tables, curated matrices, structure collections) can be registered as read-only datasets with a recorded SHA-256. Actions: 'register' hashes a file/directory and records it (fails on a missing path or checksum mismatch); 'list' shows all datasets plus whether their files still exist; 'verify' re-hashes one dataset and reports every mismatching file; 'get' resolves a dataset (or one filename inside a directory dataset) and returns its verified path; 'remove' unregisters the record but never deletes data. The command never writes to dataset contents and refuses to register paths that do not exist.

- Tool: `lifesci_datasets`
- Category: `foundation` · side effects: `write`
- Payload schema: [`schemas/datasets.schema.json`](../schemas/datasets.schema.json)
- Recommended tool timeout: 600000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `action` | enum(register\|list\|verify\|get\|remove) | no |  | Dataset registry action: 'register' a new read-only dataset with checksum; 'list' all registered datasets with existence/checksum status; 'verify' re-hash one dataset; 'get' resolve one dataset (optionally one file inside it); 'remove' unregister a dataset (never deletes data files). |
| `name` | string | no |  | Registered dataset name (required for register/verify/get/remove). |
| `path` | string | no |  | Absolute or cwd-relative path to the dataset file or directory (register only). |
| `kind` | enum(fasta\|tsv\|json\|msa\|structure\|structure_dir\|processed_dir\|table\|hmm\|hmm_dir\|generic_dir) | no |  | Dataset kind; directories are hashed file-by-file into a manifest (register only). |
| `description` | string | no |  | Free-text description of origin, version, and what the dataset contains. |
| `verify` | boolean | no | `true` | For register: hash and verify the payload before recording it (recommended). |
| `filename` | string | no |  | For get: resolve this specific file inside a registered directory dataset. |

Examples:

```json
{
  "action": "list"
}
```
```json
{
  "action": "register",
  "kind": "msa",
  "name": "reference_alignment",
  "path": "data/reference_alignment.fa"
}
```
```json
{
  "action": "verify",
  "name": "reference_alignment"
}
```
```json
{
  "action": "remove",
  "name": "reference_alignment"
}
```


## `doctor` — Check lifesci environment

Environment preflight for the lifesci tool layer. Reports Python/package versions, Foldseek/PyMOL/SSH availability, cache writability and contents, registered read-only datasets, pinned database releases, and (unless offline) reachability of every public portal. Run this first when a lifesci call fails strangely. It never fails the command because a portal is down: read data.status ('ok'|'degraded'|'offline') and data.checks.

- Tool: `lifesci_doctor`
- Category: `foundation` · side effects: `read`
- Payload schema: [`schemas/doctor.schema.json`](../schemas/doctor.schema.json)
- Recommended tool timeout: 120000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `network` | boolean | no | `true` | Probe public portals (automatically skipped in offline mode). |
| `probe_timeout_s` | number | no | `10` | Per-portal reachability timeout. |
| `portals` | array<string> | no |  | Optional subset of portal probe names to check. |
| `foldseek_path` | string | no |  | Explicit Foldseek binary path. |
| `pymol_path` | string | no |  | Explicit PyMOL executable path. |

Examples:

```json
{
  "network": false
}
```


## `domains` — Domain/family annotation and domain arbiter

Annotate proteins with domains/families and deliver an explicit per-domain verdict. Three evidence sources can be combined: (1) a cached InterPro entry-API scan for UniProt accessions; (2) recorded per-protein domain-evidence rows ('evidence_rows'/'rows_file', e.g. a recorded Pfam proteome scan) for fully offline replay; (3) a local pyhmmer hmmsearch against supplied sequences/HMMs. Each requested domain gets present/weak/absent/unknown with its E-value, the database/release and the cutoff used, so an absent call is an explicit annotation statement with its reporting threshold (e.g. 'no PF00042 hit at report E<=1000'), never proof of functional absence. The domain arbiter can compare optional sequence-level (phmmer) evidence: a significant domain with no sequence hit is reported as domain-arbiter-present because domain/family evidence outranks a single weak sequence hit. Caveats: local hmmsearch E-values scale with the number of target sequences scanned and are not directly comparable to proteome-scan or InterPro-reported values (a warning is emitted); InterPro's per-location 'score' field has portal-specific semantics and for Pfam matches is an E-value-like score.

- Tool: `lifesci_domains`
- Category: `sequence` · side effects: `network`
- Payload schema: [`schemas/domains.schema.json`](../schemas/domains.schema.json)
- Recommended tool timeout: 600000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `accessions` | array<string> | no |  | UniProt accessions for a remote InterPro scan (cached; replays offline when the InterPro payloads are already in the cache). |
| `domains` | array<string> | no |  | Domain/family accessions to arbitrate explicitly (e.g. ['PF00042']). Each gets a present/weak/absent/unknown verdict even when no annotation is returned. When omitted, every model found in the merged evidence is reported. |
| `evidence_rows` | array<object> | no |  | Recorded per-protein domain-evidence rows (offline replay). Accepts the standard presence formats: keys accession/protein/best_hit, model/pfam_model, evalue, score, model_cov, protein_length, description, source, release, target_db_size. |
| `rows_file` | string | no |  | TSV/JSON file of recorded domain-evidence rows (or a curated table). Relative paths resolve against the working directory. |
| `hmms` | array<object> | no |  | Local HMMER profile files to scan with (no remote model fetch). |
| `hmm_ids` | array<string> | no |  | Pfam model accessions to download from InterPro (wwwapi?annotation=hmm, cached) and scan locally, e.g. ['PF00042']. |
| `sequences` | object | no |  | Inline target sequences for the local HMM scan, keyed by accession. |
| `targets_fasta` | string | no |  | FASTA file of target sequences for the local HMM scan. |
| `report_evalue` | number | no | `10.0` | HMMER reporting threshold for the local scan. Use a permissive value (e.g. 1000) to state explicitly 'no hit at E<=N' for an absent domain; the cutoff is reported with the absent verdict. |
| `present_evalue` | number | no | `0.001` | E-value cutoff for a 'present' domain call (default 1e-3). |
| `weak_evalue` | number | no | `1.0` | E-value cutoff below which a domain call is 'weak' rather than absent. |
| `sequence_evidence` | object | no |  | Optional sequence-level (phmmer) evidence to compare with the domain calls, keyed by accession: {'detectable': bool\|'weak'\|'absent', 'best_evalue': number\|null, 'source': str}. A significant domain with no sequence hit is reported as domain-arbiter-present. |
| `cpus` | integer | no | `4` | CPU threads for the local pyhmmer scan. |
| `max_hits` | integer | no | `200` | Maximum recorded local HMM hits. |
| `out` | string | no |  | Optional directory for merged domain records written as JSON. |

Examples:

```json
{
  "accessions": [
    "P69905",
    "P00698"
  ],
  "domains": [
    "PF00042"
  ]
}
```
```json
{
  "domains": [
    "PF00042"
  ],
  "rows_file": "evidence/pfam_hits.tsv"
}
```
```json
{
  "domains": [
    "PF00042"
  ],
  "hmm_ids": [
    "PF00042"
  ],
  "report_evalue": 1000.0,
  "targets_fasta": "sequences/targets.fasta"
}
```


## `figures` — Generate quantitative figures and PyMOL sessions

Renders publication-style figures with the headless matplotlib Agg backend: a log-log covariance scatter with regression line and annotated outliers, a distribution histogram, or a contact-distance bar chart. Data may come from a table (JSON/TSV/CSV), inline rows, or a saved lifesci.stats payload; the source SHA-256 and regression parameters are recorded (FIGURE_DATA_PROVENANCE) so figures are reproducible. Optionally builds a PyMOL session from an opaque spec via lib.pymol_session: emits a .pml always, validates/renders with `pymol -cq` only when PyMOL is installed, and reports honest PASS/FAIL/SKIPPED. Warns PYMOL_MISSING when PyMOL is absent and PNG_RENDER_LIMITATION when headless full-session rendering fails (render small extracted structures instead).

- Tool: `lifesci_figures`
- Category: `visualisation` · side effects: `write`
- Payload schema: [`schemas/figures.schema.json`](../schemas/figures.schema.json)
- Recommended tool timeout: 600000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `figures` | array<object> | no |  | Figures to render. |
| `out_dir` | string | no |  | Directory for generated figures (default: <ctx.cwd>/lifesci-figures). |
| `pymol_spec` | object | no |  | Opaque PyMOL session spec passed verbatim to lifesci.lib.pymol_session: {name, objects:[{path, name?, load_type?, color?, selections?}], commands?, scenes?, save_pse?, render_png?:{path,width,height,dpi}}. |
| `pymol_out_dir` | string | no |  | Directory for the generated .pml/.pse session (default: <out_dir>/pymol). |

Examples:

```json
{
  "figures": [
    {
      "data": {
        "rows": [
          {
            "length": 4.0,
            "name": "sample_a",
            "score": 9.1
          },
          {
            "length": 6.4,
            "name": "sample_b",
            "score": 15.8
          },
          {
            "length": 8.1,
            "name": "sample_c",
            "score": 22.5
          },
          {
            "length": 10.2,
            "name": "sample_d",
            "score": 30.4
          }
        ]
      },
      "filename": "covariance.png",
      "group_by": "none",
      "kind": "covariance_loglog",
      "x": "length",
      "y": "score"
    }
  ]
}
```


## `http` — Raw cached HTTP request

Generic HTTP primitive for endpoints without a dedicated lifesci adapter. Goes through the same content-addressed cache, per-host rate limiting, exponential backoff, offline replay and provenance logging as every adapter. Returns request metadata, response status/headers/SHA-256/bytes/cache-hit/attempts and, when 'parse' allows it, the decoded body. In --dry-run it returns the planned request without touching the network; in --offline it replays a cached payload only. Caveats: E-values/TM-scores in arbitrary returned payloads carry the semantics of the source database (not comparable across databases, often query-normalised and top-hit capped) - this command only guarantees provenance, not scientific interpretation.

- Tool: `lifesci_http`
- Category: `foundation` · side effects: `network`
- Payload schema: [`schemas/http.schema.json`](../schemas/http.schema.json)
- Recommended tool timeout: 180000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `url` | string | yes |  | Absolute http(s) URL without query string. Query parameters go in 'params' so they are sorted deterministically, recorded in provenance, and form part of the cache key. |
| `method` | enum(GET\|HEAD\|POST\|PUT\|PATCH\|DELETE) | no | `"GET"` | HTTP method. GET/HEAD/OPTIONS are retried on transient failures; POST/PUT/PATCH/DELETE are only retried when 'idempotent' is true. |
| `params` | object | no |  | Query parameters; values are converted to strings and sorted before use. |
| `headers` | object | no |  | Extra request headers. The lifesci User-Agent is added automatically. Authorization/token headers are never cached or written to the provenance log. |
| `body` | string | no |  | Raw request body as text (UTF-8 encoded). Mutually exclusive with other body fields. |
| `body_base64` | string | no |  | Raw request body as base64 (for binary payloads). Mutually exclusive with other body fields. |
| `json_body` | ['object', 'array'] | no |  | Request body serialised as canonical JSON (sets Content-Type: application/json). |
| `form_fields` | object | no |  | urlencoded form fields. When 'files' is also given, these become the ordinary multipart/form-data fields of the upload. |
| `files` | array<object> | no |  | Multipart file uploads. Only multipart file upload works for some APIs (e.g. Foldseek web ticket); JSON bodies are rejected by those servers. |
| `cache` | boolean | no | `true` | Read/write the content-addressed cache. Set false for polling endpoints whose URL changes or that must be re-requested. |
| `cache_ttl` | number | no |  | Override the cache TTL in seconds for this response. |
| `retries` | integer | no |  | Transient retry budget for this request (overrides the context default). |
| `timeout` | number | no |  | Per-attempt timeout in seconds (overrides the context default). |
| `allow_status` | array<integer> | no | `[200]` | HTTP statuses treated as success. Non-listed statuses raise a PortalError with the status appended to the error code (e.g. HTTP_ERROR_404). |
| `database` | string | no |  | Optional database label recorded in the provenance cache entry (e.g. 'uniprot'). |
| `release_header` | string | no |  | Response header carrying the database release (e.g. 'x-uniprot-release'); its value is recorded in the release registry and provenance. |
| `idempotent` | boolean | no |  | Force or forbid retries for a write request. Defaults to true for GET/HEAD/OPTIONS and false for body-bearing methods. |
| `parse` | enum(auto\|none\|json\|text) | no | `"auto"` | Decode the response body into the payload. 'auto' parses JSON when the content type looks like JSON, otherwise returns a truncated text preview. 'none' returns metadata only (useful with 'out'). |
| `max_parse_bytes` | integer | no | `2000000` | Do not echo parsed bodies larger than this many bytes into the envelope; write them with 'out' instead. Prevents multi-megabyte tool results. |
| `text_preview_bytes` | integer | no | `4000` | Maximum characters of a text body echoed into the payload. |
| `out` | string | no |  | Optional path: write the raw response body here and record its SHA-256. Relative paths resolve against the working directory. |

Examples:

```json
{
  "parse": "text",
  "url": "https://rest.uniprot.org/uniprotkb/P69905.fasta"
}
```
```json
{
  "parse": "json",
  "url": "https://www.ebi.ac.uk/interpro/api/entry/pfam/PF00042/"
}
```


## `orthology` — Cross-check protein orthology sources

Look up family/orthogroup membership for one or more proteins across four independent sources and arbitrate them explicitly: PANTHER (via the InterPro entry API and UniProt cross-references), OrthoDB (orthogroup API), eggNOG (UniProt cross-reference; no mapper call) and OMA (OMA browser API). Returns per-source records with identifiers, labels, availability and reason, a pairwise cross-check table with agree/disagree/unknown, and an overall agreement per protein. Missing/unreachable sources are warnings, not errors; a single agreeing source is reported as unknown confidence, and any disagreement is flagged for human review. Caveats: identifier namespaces are not interchangeable across databases, agreement here means the sources carry consistent family vocabulary, not that orthology is proven; divergent fast-evolving families can be annotated by only one source.

- Tool: `lifesci_orthology`
- Category: `sequence` · side effects: `network`
- Payload schema: [`schemas/orthology.schema.json`](../schemas/orthology.schema.json)
- Recommended tool timeout: 300000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `accessions` | array<string> | yes |  | UniProt accessions to annotate. Each protein is queried across the selected orthology sources; missing sources are warnings, not errors. |
| `sources` | array<enum(panther\|orthodb\|eggnog\|oma)> | no | `["panther", "orthodb", "eggnog", "oma"]` | Which orthology sources to query. PANTHER/eggNOG come from UniProt cross-references and the InterPro entry API; OrthoDB and OMA come from their own APIs. |
| `expected_family` | string | no |  | Optional family label to test every source against. When omitted, the expected family is derived from the UniProt protein/gene name. |
| `page_size` | integer | no | `50` | InterPro entry-API page size used for the PANTHER lookup. |
| `max_pages` | integer | no | `2` | Maximum InterPro result pages fetched per protein. |
| `out` | string | no |  | Optional directory to write the orthology record JSON (per accession). |

Examples:

```json
{
  "accessions": [
    "P69905"
  ]
}
```
```json
{
  "accessions": [
    "P69905",
    "P00698"
  ],
  "expected_family": "globin"
}
```


## `remote` — Opt-in remote execution over SSH

Offload heavy compute to configured lab machines over SSH. Remote execution is OFF unless LIFESCI_REMOTE_HOSTS / LIFESCI_REMOTE_CONFIG are configured, and is never required for offline work. Actions: 'list' returns host aliases and connection metadata with no secrets (key paths never appear); 'plan' returns the ssh plan without executing; 'run' executes the job and records provenance (host, argv, start/end, exit code, remote tool version). run requires allow:true AND host+argv; use absolute tool paths because remote PATHs are minimal. stdout/stderr in the payload are truncated (use out to save the full job result). The plugin never writes credentials and never auto-executes downloaded code.

- Tool: `lifesci_remote`
- Category: `foundation` · side effects: `network`
- Payload schema: [`schemas/remote.schema.json`](../schemas/remote.schema.json)
- Recommended tool timeout: 90000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `action` | enum(list\|plan\|run) | yes |  | 'list' configured host aliases (never leaks credentials); 'plan' build the ssh job plan without executing it; 'run' execute the job over ssh (requires allow:true, and remote execution must be configured). |
| `host` | string | no |  | Configured host alias (e.g. 'gpu01'), required for plan/run. |
| `argv` | array<string> | no |  | Remote command argv (no shell string), e.g. ['/opt/hhsuite/bin/hhsearch', '-i', 'query.a3m', '-d', '/data/hhsuite/db']. Prefer absolute tool paths because remote PATHs are minimal. |
| `cwd` | string | no |  | Remote working directory for the job (optional; host work_dir otherwise). |
| `timeout` | number | no | `600` | Maximum wall-clock seconds for the remote job (default 600). |
| `allow` | boolean | no | `false` | Explicit opt-in required for action='run'. Without it, run refuses; use action='plan' or --dry-run to inspect the job first. |
| `out` | string | no |  | Optional path to write the full job result JSON (stdout/stderr are truncated in data). |

Examples:

```json
{
  "action": "list"
}
```
```json
{
  "action": "plan",
  "argv": [
    "/opt/hhsuite/bin/hhsearch",
    "-h"
  ],
  "host": "gpu01"
}
```


## `report` — Assemble an evidence-graded Markdown report

Builds a reproducible Markdown report from structured, evidence-graded claims. Every claim MUST carry a grade (ESTABLISHED, INFERRED, UNKNOWN) and ESTABLISHED/INFERRED claims MUST carry at least one PMID/DOI/URL citation; the command refuses otherwise (EVIDENCE_GRADE_MISSING / EVIDENCE_CITATION_MISSING) and never silently downgrades a grade. Output includes the per-section claims, an explicit caveats list, and a provenance table assembled from the command envelope provenance (HTTP fetches with URL/SHA-256/release, local file hashes, tool runs) plus any records supplied in 'provenance'. Deterministic for identical input. Quantitative claims should state value, n, test, measure definition, database/version, and date.

- Tool: `lifesci_report`
- Category: `reporting` · side effects: `write`
- Payload schema: [`schemas/report.schema.json`](../schemas/report.schema.json)
- Recommended tool timeout: 120000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `title` | string | yes |  | Report title. |
| `sections` | array<object> | yes |  | Report sections, each with graded claims. |
| `caveats` | array<string> | no |  | Explicit caveats/limitations (e.g. 'no detectable homolog is not proof of absence', 'correlation is not causation'). Recommended; a warning is emitted when empty. |
| `provenance` | array<object> | no |  | Additional provenance records (kind/time/url/sha256/release...) to include in the table. |
| `include_envelope_provenance` | boolean | no | `true` | Include the current command envelope's provenance records in the report provenance table. |
| `generated_at` | string | no |  | Deterministic generation timestamp rendered in the report. Default: the latest timestamp among the included provenance records, else 'not-recorded'. Pass an explicit ISO-8601 value for reproducible report bytes. |
| `out` | string | no |  | Markdown output path (default: <ctx.cwd>/lifesci-report.md). |

Examples:

```json
{
  "caveats": [
    "Example caveat: correlation is not causation."
  ],
  "sections": [
    {
      "claims": [
        {
          "citations": [
            {
              "doi": "10.1000/example.1",
              "title": "Example reference"
            }
          ],
          "grade": "ESTABLISHED",
          "id": "C1",
          "text": "Example claim supported by one citation."
        }
      ],
      "heading": "Example section"
    }
  ],
  "title": "Example evidence-graded report"
}
```


## `seq.fetch` — Fetch UniProt sequences, entries and proteomes

Fetch UniProt data reproducibly: per-accession FASTA sequences (returned inline and written to 'out'), optional normalised entry metadata (protein name, organism/taxid, cross-references to PANTHER/eggNOG/OrthoDB/OMA/AlphaFold, features, comments, references), and reference-proteome FASTAs. The UniProt release is recorded for every payload; REST proteome streaming falls back to the release-pinned FTP/EBI reference-proteome FASTA when rate-limited or empty (a warning tells you which path was used; non-reference proteomes may not exist on FTP). Failures for individual accessions are returned in errors[] while the remaining fetches continue. Caveats: protein E-values and BLAST/HMMER counts computed against different UniProt releases or proteome sets are not strictly comparable - always carry the recorded release forward.

- Tool: `lifesci_seq_fetch`
- Category: `sequence` · side effects: `network`
- Payload schema: [`schemas/seq.fetch.schema.json`](../schemas/seq.fetch.schema.json)
- Recommended tool timeout: 600000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `accessions` | array<string> | no |  | UniProt accessions to fetch. Each yields a FASTA sequence record; the same accession is also looked up as an entry when 'fetch_entries' is true. Unknown accessions are reported in errors[] without failing the other fetches. |
| `fetch_entries` | boolean | no | `false` | Also fetch and normalise the full UniProtKB entry (protein name, organism, length, cross-references, features, comments). Needed by downstream orthology and provenance reporting; costs one request per accession. |
| `proteomes` | array<object> | no |  | Reference proteomes to download. Provide either 'proteome_id' (UP...) or 'taxid' (resolved through the UniProt proteomes API). The REST stream is tried first; on HTTP 429/5xx/empty it falls back to the release-pinned EBI/FTP reference-proteome FASTA and warns (some non-reference proteomes are only available via REST). |
| `out` | string | no |  | Output directory for sequence FASTA files and downloaded proteomes (default: <cache-dir>/out/seq.fetch). Relative paths resolve against the working directory. |
| `write_sequences` | boolean | no | `true` | Write one <accession>.fasta file per fetched sequence into 'out'. Disable for a pure metadata query; the sequence is still returned inline in sequences[]. |
| `strict` | boolean | no | `false` | Raise on the first accession/proteome failure instead of collecting failures in errors[] and continuing. The command always raises when every requested item fails. |

Examples:

```json
{
  "accessions": [
    "P69905"
  ],
  "fetch_entries": true
}
```
```json
{
  "proteomes": [
    {
      "proteome_id": "UP000005640",
      "taxid": 9606
    }
  ]
}
```


## `seq.search` — Homolog search and presence matrix

Detect homologs and build a presence/absence matrix with per-cell evidence. Modes: 'phmmer' runs local pyhmmer phmmer (compositional-bias filter OFF by design; reciprocal best-hit checks) and can add a Pfam/HMM domain scan that curates cells; 'hmmsearch' scans profile HMMs against proteomes and curates them with caller-supplied factor signatures (unmapped models become factors named by model id); 'evidence' replays recorded per-model Pfam hit rows (pfam_hits.tsv format), optional sequence rows and reciprocal-check rows offline and curates a presence matrix with evidence strings. Factors and their models are declared via factor_queries / factor_models. Mandatory caveats are always reported: absent means 'no detectable homolog' not proof of absence; E-values depend on database size and are not comparable across proteomes/databases; single-sequence search misses very divergent proteins, so domain/family evidence is the arbiter; weak calls are flagged for human review. Reciprocal checks are reported when available; without them sequence-level exclusions are not corroborated.

- Tool: `lifesci_seq_search`
- Category: `sequence` · side effects: `compute`
- Payload schema: [`schemas/seq.search.schema.json`](../schemas/seq.search.schema.json)
- Recommended tool timeout: 600000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `mode` | enum(phmmer\|hmmsearch\|evidence) | yes |  | Search backend. 'phmmer' runs pyhmmer phmmer (bias_filter=False) and optional reciprocal/domain checks; 'hmmsearch' scans profile HMMs against proteomes; 'evidence' replays recorded per-model hit rows offline and curates a presence matrix. |
| `queries` | object | no |  | phmmer queries as {name: protein_sequence}. Prefer descriptive names such as 'globin:P69905'; pair them with 'factor_queries' for a presence matrix. |
| `query_accessions` | array<string> | no |  | UniProt accessions fetched and used as phmmer queries (one request each). |
| `queries_file` | string | no |  | FASTA file of phmmer queries (header first token = query name). |
| `targets` | array<object> | no |  | Target proteomes/FASTAs for phmmer/hmmsearch. For presence curation each target should carry an 'organism' label. |
| `hmms` | array<object> | no |  | Local HMM models for the domain scan (also used by hmmsearch mode). |
| `hmm_ids` | array<string> | no |  | Pfam model accessions (e.g. PF00042) downloaded from InterPro (cached) for the local domain scan. |
| `E` | number | no | `10.0` | HMMER reporting E-value threshold for sequence/domain searches. |
| `cpus` | integer | no | `4` | CPU threads for pyhmmer searches. |
| `max_hits` | integer | no | `100` | Maximum hits kept per search. |
| `bias_filter` | boolean | no | `false` | Compositional-bias filter. Keep false (default): the filter can hide genuine divergent homologues. Setting true emits a stronger warning. |
| `reciprocal` | boolean | no | `true` | phmmer mode only: run the reverse search of forward best hits against the query database and report reciprocal best hits. Required for excluding homologues. |
| `factor_queries` | object | no |  | Presence-matrix design: {factor: [query name, ...]} mapping the phmmer query names to factors (e.g. {'globin': ['globin:P69905']}). Required to build cells/matrix in phmmer mode without a domain scan. |
| `factor_models` | object | no |  | Optional mapping of factor -> Pfam model accessions used for curation, e.g. {'globin': ['PF00042']}. There are no built-in factors: supply factor_models (or factor_queries) to curate a presence matrix. |
| `rows` | array<object> | no |  | evidence mode: inline recorded per-model hit rows (pfam_hits.tsv columns: organism, taxid, proteome, pfam_model, model_name, best_hit, evalue, score, model_cov, protein_length, description). Multiple rows per organism/model are expected. |
| `rows_file` | oneOf | no |  | evidence mode: TSV/JSON file of recorded per-model hit rows, or a list of such files. |
| `cells` | array<object> | no |  | evidence mode: explicit cells [{organism, factor, hits: [{protein, model, evalue, ...}], sequence_hit?}]. Used instead of grouping flat rows. |
| `sequence_rows` | array<object> | no |  | evidence mode: inline phmmer sequence-hit rows (presence_phmmer.tsv columns: organism, factor, best_hit, evalue, score, description, verdict) attached to cells as sequence_hit. |
| `sequence_rows_file` | string | no |  | evidence mode: TSV file of phmmer sequence-hit rows. |
| `reciprocal_rows` | array<object> | no |  | evidence mode: inline reciprocal-check rows (reciprocal_check.tsv columns). |
| `reciprocal_file` | string | no |  | evidence mode: TSV file of reciprocal-check rows (reciprocal_check.tsv format). |
| `expected` | object | no |  | evidence mode: expected result to verify reproduction, e.g. {'matrix': {org: {factor: verdict}}, 'evidence': {org: {factor: 'string'}}} or a cells list. Mismatches are reported and warned. |
| `expected_file` | string | no |  | evidence mode: JSON file with the expected matrix/evidence. |
| `present_evalue` | number | no | `0.001` | E-value cutoff for a 'present' presence call (sequence-only matrices; default 1e-3). |
| `weak_evalue` | number | no | `1.0` | E-value cutoff below which a call is 'weak' rather than absent. |
| `out` | string | no |  | Optional directory for written hits TSV / matrix JSON. |

Examples:

```json
{
  "factor_models": {
    "globin": [
      "PF00042"
    ]
  },
  "mode": "evidence",
  "rows": [
    {
      "best_hit": "P69905",
      "evalue": 1e-30,
      "factor": "globin",
      "model_cov": 0.95,
      "organism": "H. sapiens",
      "pfam_model": "PF00042",
      "protein_length": 142,
      "score": 120.0
    }
  ]
}
```
```json
{
  "factor_queries": {
    "globin": [
      "globin:P69905"
    ]
  },
  "mode": "phmmer",
  "queries": {
    "globin:P69905": "MVLSPADKTNVKAAWGKVGAHAGEYGAEALERMFLSFPTTKTYFPHFDLSHGSAQVKGHGKKVADALTNAVAHVDDMPNALSALSDLHAHKLRVDPVNFKLLSHCLLVTLAAHLPAEFTPAVHASLDKFLASVSTVLTSKYR"
  },
  "targets": [
    {
      "fasta": "proteomes/UP000005640.fasta",
      "organism": "H. sapiens"
    }
  ]
}
```
```json
{
  "hmm_ids": [
    "PF00042"
  ],
  "mode": "hmmsearch",
  "targets": [
    {
      "fasta": "proteomes/UP000005640.fasta",
      "organism": "H. sapiens"
    }
  ]
}
```


## `stats` — Correlation, partial correlation, and log-log regression

Statistical association between two numeric columns of a small table. Reports n, Pearson r (with p), Spearman rho (with p), an optional partial correlation controlling for a nuisance variable (control='auto' uses an existing 'core' column; control='core' derives it from core_from, i.e. total_column minus segment_columns), a log-log fit log(y)=a+slope*log(x), per-group correlations, and standardised-residual outliers. group_by='auto' groups identifiers by the prefix before the first '_' or '-'. Always emits CORRELATION_NOT_CAUSATION and PHYLOGENETIC_NON_INDEPENDENCE caveats, plus SMALL_N below the threshold. Row filters: name_prefix, name_suffix, name_contains, name_regex, ids, and per-column value filters.

- Tool: `lifesci_stats`
- Category: `statistics` · side effects: `read`
- Payload schema: [`schemas/stats.schema.json`](../schemas/stats.schema.json)
- Recommended tool timeout: 120000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `table` | string | no |  | Path to a table: JSON (object-of-records keyed by name, list of records, or {'rows': [...]}), TSV, or CSV. Relative paths resolve against ctx.cwd. |
| `rows` | array<object> | no |  | Inline records (alternative to table). |
| `id_column` | string | no |  | Column used as the point identifier/label. Default: JSON object keys, else the first of name/id/key/label/organism present, else row_NNNN. |
| `x` | string | yes |  | Numeric column for the x variable. Log-log fit uses log(x). |
| `y` | string | yes |  | Numeric column for the y variable. Log-log fit uses log(y). |
| `control` | ['string', 'null'] | no |  | Column for the partial correlation controlling variable, or the special value 'core'. Default 'auto': use an existing 'core' column if present, else no partial correlation. 'core' without an existing column requires 'core_from' (total_column + segment_columns). Null disables the partial correlation. |
| `core_from` | object | no |  | Definition used when control='core': core = total - sum(segment columns). |
| `filter` | object | no |  | Row selection applied before correlation (all listed conditions must match). |
| `group_by` | string | no |  | Column used to split per-group correlations. Default 'auto' groups by the identifier prefix before the first '_' or '-' (lowercased); 'none' disables groups; any other value must be an existing column name. |
| `small_n_threshold` | integer | no | `100` | Emit the SMALL_N caveat when the number of valid points is below this (default 100: cross-species correlations with dozens of species are small). |
| `outlier_z` | number | no | `2.0` | Absolute standardised-residual cutoff used to label outliers. |
| `include_points` | boolean | no | `true` | Include the per-point (label, x, y, group, control) table in the payload. |
| `out` | string | no |  | Optional path to write the statistics payload JSON. |

Examples:

```json
{
  "rows": [
    {
      "name": "sample_a",
      "x": 4.0,
      "y": 9.1
    },
    {
      "name": "sample_b",
      "x": 6.4,
      "y": 15.8
    },
    {
      "name": "sample_c",
      "x": 8.1,
      "y": 22.5
    },
    {
      "name": "sample_d",
      "x": 10.2,
      "y": 30.4
    }
  ],
  "x": "x",
  "y": "y"
}
```
```json
{
  "group_by": "none",
  "table": "tables/measurements.tsv",
  "x": "length",
  "y": "score"
}
```


## `struct.fetch` — Fetch structure models

Fetch protein structure models/structures reproducibly. AlphaFold DB entries come with their model version and file URLs; when AFDB has no model (common for TrEMBL entries) the command attempts an ESMFold single-sequence prediction and then, if that also fails, reports an explicit 'no model' verdict in unavailable[] with warnings - an unavailable model is NOT evidence of absence. RCSB PDB mmCIF files and EMDB density maps (large; warning) are also supported. Every written file carries a SHA-256 recorded in provenance, and the model version/release is returned for each available structure. Caveats: AlphaFold/ESMFold models are predictions, not experimental structures; ESMFold is limited to short sequences (HTTP 413 for very long proteins) and can time out; per-residue confidence (pLDDT) is not evaluated here.

- Tool: `lifesci_struct_fetch`
- Category: `structure` · side effects: `network`
- Payload schema: [`schemas/struct.fetch.schema.json`](../schemas/struct.fetch.schema.json)
- Recommended tool timeout: 600000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `accessions` | array<string> | no |  | UniProt accessions whose AlphaFold DB models to fetch. AFDB has no model for many TrEMBL entries; those entries go through the ESMFold fallback (when enabled) and, if that fails too, are reported in unavailable[] with an explicit 'no model' verdict. |
| `pdb_ids` | array<string> | no |  | RCSB PDB IDs (e.g. 1UBQ) to download as experimental structures. |
| `emdb_ids` | array<string> | no |  | EMDB map IDs (e.g. EMD-1234). Maps are large; they are written to 'out' only. |
| `out` | string | no |  | Output directory for downloaded model/structure files (default <cache-dir>/out/struct.fetch). Relative paths resolve against the working directory. Every file gets a recorded SHA-256. |
| `afdb_format` | enum(pdb\|cif) | no | `"pdb"` | File format for AlphaFold DB model downloads (pdb or cif). |
| `pdb_format` | enum(cif\|pdb) | no | `"cif"` | File format for RCSB PDB downloads (mmCIF is the canonical format). |
| `esmfold_fallback` | boolean | no | `true` | When an AlphaFold DB model is unavailable, fetch the UniProt sequence and attempt an ESMFold prediction (single-sequence, not an experimentally validated structure). The fallback attempt and its outcome are always reported; ESMFold returns HTTP 413 for very long proteins and can time out (504). |

Examples:

```json
{
  "accessions": [
    "P69905"
  ]
}
```
```json
{
  "out": "structures",
  "pdb_ids": [
    "1UBQ"
  ]
}
```


## `struct.search` — Foldseek structural search

Structural-similarity search with explicit interpretation limits. Modes: 'panel' runs the local Foldseek binary on a controlled panel and calibrates the background with negative controls (unrelated proteins of the same fold class); only scores above control_max_tm + margin are interpretable. 'web' runs the Foldseek web API (AFDB50/AFDB-SwissProt/PDB100; only multipart PDB upload works). 'from_table' replays recorded hit tables offline and optionally checks caller-supplied sanity anchors (expected_anchors), warning with SANITY_ANCHOR_MISSING when one is not recovered. Always reported: TM-scores are query-normalised (fragment/short queries score higher), result lists are capped (AFDB50 top ~1000 per database), and E-values are database-size dependent, so E-values must never be compared across databases. Structural similarity supports 'closest structural neighbour' but is not automatic orthology; use reciprocal searches and domain/family evidence as the arbiter.

- Tool: `lifesci_struct_search`
- Category: `structure` · side effects: `network`
- Payload schema: [`schemas/struct.search.schema.json`](../schemas/struct.search.schema.json)
- Recommended tool timeout: 600000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `mode` | enum(panel\|web\|from_table) | yes |  | Search backend: 'panel' = local Foldseek on a controlled panel with negative controls; 'web' = Foldseek web API against AFDB50/SwissProt/PDB100; 'from_table' = offline replay of recorded hit tables with sanity-anchor checks. |
| `queries` | array<oneOf> | no |  | panel mode: query structures as {"id", "path"} objects or path strings. |
| `targets` | oneOf | no |  | panel mode: the target panel — a directory path, {"dir": path}, or a list of structure paths. Candidate homologues and negative controls should be in the panel. |
| `controls` | array<oneOf> | no |  | panel mode: negative-control structures of the same fold class (unrelated proteins). They are searched against the same panel; the maximum control TM is the background and hits must exceed control_max_tm + margin to be interpretable. |
| `margin` | number | no | `0.05` | panel mode: margin above the control maximum before a TM score is interpretable. |
| `min_control_coverage` | number | no | `0.3` | panel mode: minimum query coverage (qcov) for a negative-control alignment to count towards the background. Foldseek can give a high alignment TM for a 10-residue fragment; such fragments are not a meaningful background. |
| `min_hit_coverage` | number | no | `0.3` | panel mode: minimum query coverage (qcov) for a query hit to be interpretable. Hits below this are reported but flagged as low-coverage, not called homologous. |
| `fmt` | string | no | `"query,target,alntmscore,qtmscore,ttmscore,lddt,evalue,qcov,tcov,alnlen"` | panel mode: Foldseek --format-output string. |
| `e` | number | no | `100.0` | panel mode: Foldseek E-value reporting threshold. |
| `threads` | integer | no | `4` | panel mode: Foldseek threads. |
| `binary` | string | no |  | Explicit Foldseek binary path (default: $LIFESCI_FOLDSEEK, tools/bin, then PATH). |
| `pdb_paths` | array<string> | no |  | web mode: local PDB model files uploaded as queries (only PDB upload works; FASTA/JSON fail). |
| `pdb_accessions` | array<string> | no |  | web mode: UniProt accessions whose AlphaFold DB PDB models are used as queries. |
| `databases` | array<enum(afdb50\|afdb-swissprot\|pdb100)> | no | `["afdb50"]` | web mode: Foldseek databases to search. AFDB50 is the global sensitive AFDB search. |
| `taxfilter` | string | no |  | web mode: optional NCBI taxonomy filter (applied only when set). |
| `poll_interval` | number | no | `5` | web mode: seconds between ticket polls. |
| `max_wait` | number | no | `600` | web mode: maximum seconds to wait for a Foldseek web job. |
| `tables` | oneOf | no |  | from_table mode: recorded hit table(s) — path strings or {"path", "query", "database", "format"}. |
| `hits` | array<object> | no |  | from_table mode: inline recorded hits instead of files. Each row may carry query/db/rank/target_accession/tm_score/prob/evalue/... (web format) or query/target/alntmscore/... (panel format). |
| `expected_anchors` | array<object> | no |  | Anchors that must be recovered for the search to be considered sane. There are no built-in anchors: supply the expected target(s) explicitly. |
| `out` | string | no |  | Optional directory: combined hits TSV + search summary JSON + anchor report are written here. |
| `db_metadata` | object | no |  | Optional extra database/release metadata to record with the result. |

Examples:

```json
{
  "expected_anchors": [
    {
      "min_tm": 0.5,
      "required": true,
      "target_accession": "P00698"
    }
  ],
  "mode": "from_table",
  "tables": [
    "hits/structural_search.tsv"
  ]
}
```
```json
{
  "controls": [
    "controls/control_a.pdb",
    "controls/control_b.pdb"
  ],
  "mode": "panel",
  "queries": [
    "models/query_model.pdb"
  ],
  "targets": {
    "dir": "panel/"
  }
}
```
```json
{
  "databases": [
    "afdb50"
  ],
  "mode": "web",
  "pdb_accessions": [
    "P69905"
  ]
}
```


## `struct.superpose` — Superpose structures (Kabsch on alignment columns, or Foldseek TM)

Structural comparison with two modes. kabsch: superpose structures on shared alignment columns (from colmap residue→column maps), optionally excluding spans such as a variable region; returns per-structure rotation/translation into the reference frame, core RMSD and local RMSD near the focus spans, plus absent columns. Warns FEW_CORE_COLUMNS and raises NO_SHARED_COLUMNS when too few columns are shared. foldseek: pairwise structural alignment via the local Foldseek binary, reporting TM/LDDT/e-value/coverage per pair. Always treats TM as query-length normalised and E-values as database-size dependent: run reciprocal searches and never compare E-values across databases. Trimmed structures are valid inputs; use the preserved residue numbering and colmap.

- Tool: `lifesci_struct_superpose`
- Category: `structure` · side effects: `compute`
- Payload schema: [`schemas/struct.superpose.schema.json`](../schemas/struct.superpose.schema.json)
- Recommended tool timeout: 600000 ms

| input field | type | required | default | description |
|---|---|---|---|---|
| `mode` | enum(kabsch\|foldseek) | no | `"kabsch"` | 'kabsch': column-based superposition (requires a colmap per structure; reports transforms and core/local RMSD). 'foldseek': pairwise TM/LDDT alignment using the local Foldseek binary. |
| `structures` | array<object> | yes |  | Structures to compare; ids must be unique. |
| `reference` | string | no |  | kabsch: id of the reference structure (default: first structure). Transforms map others into its frame. |
| `exclude_spans` | array<array<integer>> | no |  | kabsch: alignment-column spans [[start,end], ...] excluded from the core (e.g. a variable region, so the fit is not driven by it). |
| `core_spans` | array<array<integer>> | no |  | kabsch: restrict the core to these alignment-column spans (inclusive). |
| `core_columns` | array<integer> | no |  | kabsch: explicit alignment columns to use as core (intersected with the shared/modelled set). |
| `focus_spans` | array<array<integer>> | no |  | kabsch: spans defining the insertion-site neighbourhood for local RMSD (local core columns are those within local_window columns of any focus span). Default: exclude_spans. |
| `local_window` | integer | no | `600` | kabsch: half-window in alignment columns around focus_spans used for the local RMSD. |
| `binary` | string | no |  | foldseek: explicit Foldseek binary path (default: LIFESCI_FOLDSEEK / tools/bin / $PATH). |
| `threads` | integer | no | `4` | foldseek: CPU threads for the local search. |
| `e` | number | no | `100` | foldseek: E-value cutoff for the local search (E-values are database-size dependent). |
| `out` | string | no |  | Optional directory for Foldseek intermediates and/or the payload JSON (a .json suffix writes JSON). |

Examples:

```json
{
  "exclude_spans": [
    [
      10,
      20
    ]
  ],
  "mode": "kabsch",
  "reference": "structure_a",
  "structures": [
    {
      "colmap": "structures/structure_a_colmap.json",
      "id": "structure_a",
      "path": "structures/structure_a.cif"
    },
    {
      "colmap": "structures/structure_b_colmap.json",
      "id": "structure_b",
      "path": "structures/structure_b.cif"
    }
  ]
}
```


