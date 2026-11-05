# Agent tool reference (`lifesci_*`)

Generated from the Python command registry by `scripts/generate_docs.py`. The TypeScript host plugin registers exactly these tools; parameter schemas are identical to the CLI input schemas (see [commands.md](commands.md)).

| tool | command | timeout | purpose |
|---|---|---|---|
| `lifesci_align_map` | `align.map` | 300000 ms | Project query sequences onto a reference alignment row; report identity, coverage, feature lengths. |
| `lifesci_cache` | `cache` | 300000 ms | Report cache stats, list/verify entries, prune expired payloads, or clear the cache. |
| `lifesci_contacts` | `contacts` | 600000 ms | Minimum atom-atom distances, closest atom pairs, percentiles, missing features, and bridges. |
| `lifesci_datasets` | `datasets` | 600000 ms | Register, list, verify, resolve, or unregister checksum-pinned local reference data. |
| `lifesci_doctor` | `doctor` | 120000 ms | Report dependency versions, cache health, tool availability, and portal reachability. |
| `lifesci_domains` | `domains` | 600000 ms | InterPro/Pfam domain scan, recorded-evidence replay, and/or local HMM scan with explicit domain-absent verdicts. |
| `lifesci_figures` | `figures` | 600000 ms | Render log-log covariance / histogram / contact-bar figures with matplotlib Agg, and build PyMOL sessions. |
| `lifesci_http` | `http` | 180000 ms | Perform one retrying/caching/rate-limited HTTP request with full provenance. |
| `lifesci_orthology` | `orthology` | 300000 ms | Query PANTHER, OrthoDB, eggNOG and OMA and report an agree/disagree/unknown cross-check table. |
| `lifesci_remote` | `remote` | 90000 ms | List configured SSH hosts, plan a remote job, or run it with explicit opt-in. |
| `lifesci_report` | `report` | 120000 ms | Render graded claims (ESTABLISHED/INFERRED/UNKNOWN + PMID/DOI) with caveats and a provenance table. |
| `lifesci_seq_fetch` | `seq.fetch` | 600000 ms | Fetch UniProtKB FASTA sequences, normalised entry metadata, and reference proteomes with release pinning. |
| `lifesci_seq_search` | `seq.search` | 600000 ms | pyhmmer phmmer/hmmsearch or offline replay of recorded evidence, curated into a presence matrix. |
| `lifesci_stats` | `stats` | 120000 ms | Compute Pearson/Spearman/partial correlations, a log-log fit, group trends, and outliers. |
| `lifesci_struct_fetch` | `struct.fetch` | 600000 ms | Download AlphaFold DB models (with ESMFold fallback), PDB structures and EMDB maps; record SHA-256. |
| `lifesci_struct_search` | `struct.search` | 600000 ms | Controlled local Foldseek panel with negative-control calibration, Foldseek web API, or recorded-table replay with sanity anchors. |
| `lifesci_struct_superpose` | `struct.superpose` | 600000 ms | Column-based superposition with transforms/core/local RMSD, or pairwise Foldseek TM alignment. |

## `lifesci_align_map`

Aligns each query to a reference row of an MSA using tuned PairwiseAligner settings (match +2, mismatch -1, gap open -8, extend -0.5, cheap end gaps), records the residue→column map, and counts query residues inside feature column spans or reference-row coordinate ranges. Returns per query: length, n_aligned, identity, coverage, per-feature lengths + residues, and flags. Warns LOW_IDENTITY_MAPPING below min_identity (default 0.60) and FEATURE_SPAN_UNSTABLE because feature-span column counts are unreliable at low identity — always report identity with the lengths. reference_row='auto' tries every candidate row per query and keeps the highest-identity one (restrict with reference_rows); the chosen row is reported per query. Use feature_spans (0-based inclusive alignment columns) or feature_coords (1-based ungapped reference-row positions).

Maps to `lifesci align.map`. Parameters are the input schema in [commands.md](commands.md#alignmap--map-sequences-onto-a-reference-msa-row-and-measure-feature-s).

## `lifesci_cache`

Maintenance for the content-addressed HTTP cache that makes lifesci runs replayable offline. Actions: 'stats' (counts/bytes/databases/expired), 'list' (deterministic per-entry metadata), 'verify' (re-hash bodies; reports corruption), 'prune' (delete expired entries; honour dry_run), 'clear' (delete everything; requires confirm:true). This command never touches the network, so it is safe under --offline. Clearing the cache destroys the ability to reproduce prior results offline; verify first and export anything you need. Entries are sorted by cache key for deterministic output.

Maps to `lifesci cache`. Parameters are the input schema in [commands.md](commands.md#cache--inspect-and-maintain-the-lifesci-cache).

## `lifesci_contacts`

Measures minimum atom-atom distances between named structural features across one or more structures. RNA features may be selected by explicit residues ('101-120') or by alignment-column spans resolved through a residue→column colmap JSON; protein features use all atoms by default. Returns per pair: min_distance, closest atom/residue pair, n_under counts for thresholds (default 4/5/10 Å), and the percentile position of the contact along each element; missing features are reported explicitly, and bridges through a third feature trigger CONTACT_BRIDGE_THROUGH_THIRD_FEATURE. Always warns DISTANCE_ATOM_SELECTION (the value depends on the atom selection; P atoms ignore side chains) and CONTACT_ABSENT_NOT_PROOF (no contact in these structures/thresholds is not evidence of no interaction).

Maps to `lifesci contacts`. Parameters are the input schema in [commands.md](commands.md#contacts--inter-element-contact-distances).

## `lifesci_datasets`

Local reference data (alignments, ES-length tables, curated matrices, structure collections) can be registered as read-only datasets with a recorded SHA-256. Actions: 'register' hashes a file/directory and records it (fails on a missing path or checksum mismatch); 'list' shows all datasets plus whether their files still exist; 'verify' re-hashes one dataset and reports every mismatching file; 'get' resolves a dataset (or one filename inside a directory dataset) and returns its verified path; 'remove' unregisters the record but never deletes data. The command never writes to dataset contents and refuses to register paths that do not exist.

Maps to `lifesci datasets`. Parameters are the input schema in [commands.md](commands.md#datasets--manage-local-read-only-datasets).

## `lifesci_doctor`

Environment preflight for the lifesci tool layer. Reports Python/package versions, Foldseek/PyMOL/SSH availability, cache writability and contents, registered read-only datasets, pinned database releases, and (unless offline) reachability of every public portal. Run this first when a lifesci call fails strangely. It never fails the command because a portal is down: read data.status ('ok'|'degraded'|'offline') and data.checks.

Maps to `lifesci doctor`. Parameters are the input schema in [commands.md](commands.md#doctor--check-lifesci-environment).

## `lifesci_domains`

Annotate proteins with domains/families and deliver an explicit per-domain verdict. Three evidence sources can be combined: (1) a cached InterPro entry-API scan for UniProt accessions; (2) recorded per-protein domain-evidence rows ('evidence_rows'/'rows_file', e.g. a recorded Pfam proteome scan) for fully offline replay; (3) a local pyhmmer hmmsearch against supplied sequences/HMMs. Each requested domain gets present/weak/absent/unknown with its E-value, the database/release and the cutoff used, so an absent call is an explicit annotation statement with its reporting threshold (e.g. 'no PF00042 hit at report E<=1000'), never proof of functional absence. The domain arbiter can compare optional sequence-level (phmmer) evidence: a significant domain with no sequence hit is reported as domain-arbiter-present because domain/family evidence outranks a single weak sequence hit. Caveats: local hmmsearch E-values scale with the number of target sequences scanned and are not directly comparable to proteome-scan or InterPro-reported values (a warning is emitted); InterPro's per-location 'score' field has portal-specific semantics and for Pfam matches is an E-value-like score.

Maps to `lifesci domains`. Parameters are the input schema in [commands.md](commands.md#domains--domain-family-annotation-and-domain-arbiter).

## `lifesci_figures`

Renders publication-style figures with the headless matplotlib Agg backend: a log-log covariance scatter with regression line and annotated outliers, a distribution histogram, or a contact-distance bar chart. Data may come from a table (JSON/TSV/CSV), inline rows, or a saved lifesci.stats payload; the source SHA-256 and regression parameters are recorded (FIGURE_DATA_PROVENANCE) so figures are reproducible. Optionally builds a PyMOL session from an opaque spec via lib.pymol_session: emits a .pml always, validates/renders with `pymol -cq` only when PyMOL is installed, and reports honest PASS/FAIL/SKIPPED. Warns PYMOL_MISSING when PyMOL is absent and PNG_RENDER_LIMITATION when headless full-session rendering fails (render small extracted structures instead).

Maps to `lifesci figures`. Parameters are the input schema in [commands.md](commands.md#figures--generate-quantitative-figures-and-pymol-sessions).

## `lifesci_http`

Generic HTTP primitive for endpoints without a dedicated lifesci adapter. Goes through the same content-addressed cache, per-host rate limiting, exponential backoff, offline replay and provenance logging as every adapter. Returns request metadata, response status/headers/SHA-256/bytes/cache-hit/attempts and, when 'parse' allows it, the decoded body. In --dry-run it returns the planned request without touching the network; in --offline it replays a cached payload only. Caveats: E-values/TM-scores in arbitrary returned payloads carry the semantics of the source database (not comparable across databases, often query-normalised and top-hit capped) - this command only guarantees provenance, not scientific interpretation.

Maps to `lifesci http`. Parameters are the input schema in [commands.md](commands.md#http--raw-cached-http-request).

## `lifesci_orthology`

Look up family/orthogroup membership for one or more proteins across four independent sources and arbitrate them explicitly: PANTHER (via the InterPro entry API and UniProt cross-references), OrthoDB (orthogroup API), eggNOG (UniProt cross-reference; no mapper call) and OMA (OMA browser API). Returns per-source records with identifiers, labels, availability and reason, a pairwise cross-check table with agree/disagree/unknown, and an overall agreement per protein. Missing/unreachable sources are warnings, not errors; a single agreeing source is reported as unknown confidence, and any disagreement is flagged for human review. Caveats: identifier namespaces are not interchangeable across databases, agreement here means the sources carry consistent family vocabulary, not that orthology is proven; divergent fast-evolving families can be annotated by only one source.

Maps to `lifesci orthology`. Parameters are the input schema in [commands.md](commands.md#orthology--cross-check-protein-orthology-sources).

## `lifesci_remote`

Offload heavy compute to configured lab machines over SSH. Remote execution is OFF unless LIFESCI_REMOTE_HOSTS / LIFESCI_REMOTE_CONFIG are configured, and is never required for offline work. Actions: 'list' returns host aliases and connection metadata with no secrets (key paths never appear); 'plan' returns the ssh plan without executing; 'run' executes the job and records provenance (host, argv, start/end, exit code, remote tool version). run requires allow:true AND host+argv; use absolute tool paths because remote PATHs are minimal. stdout/stderr in the payload are truncated (use out to save the full job result). The plugin never writes credentials and never auto-executes downloaded code.

Maps to `lifesci remote`. Parameters are the input schema in [commands.md](commands.md#remote--opt-in-remote-execution-over-ssh).

## `lifesci_report`

Builds a reproducible Markdown report from structured, evidence-graded claims. Every claim MUST carry a grade (ESTABLISHED, INFERRED, UNKNOWN) and ESTABLISHED/INFERRED claims MUST carry at least one PMID/DOI/URL citation; the command refuses otherwise (EVIDENCE_GRADE_MISSING / EVIDENCE_CITATION_MISSING) and never silently downgrades a grade. Output includes the per-section claims, an explicit caveats list, and a provenance table assembled from the command envelope provenance (HTTP fetches with URL/SHA-256/release, local file hashes, tool runs) plus any records supplied in 'provenance'. Deterministic for identical input. Quantitative claims should state value, n, test, measure definition, database/version, and date.

Maps to `lifesci report`. Parameters are the input schema in [commands.md](commands.md#report--assemble-an-evidence-graded-markdown-report).

## `lifesci_seq_fetch`

Fetch UniProt data reproducibly: per-accession FASTA sequences (returned inline and written to 'out'), optional normalised entry metadata (protein name, organism/taxid, cross-references to PANTHER/eggNOG/OrthoDB/OMA/AlphaFold, features, comments, references), and reference-proteome FASTAs. The UniProt release is recorded for every payload; REST proteome streaming falls back to the release-pinned FTP/EBI reference-proteome FASTA when rate-limited or empty (a warning tells you which path was used; non-reference proteomes may not exist on FTP). Failures for individual accessions are returned in errors[] while the remaining fetches continue. Caveats: protein E-values and BLAST/HMMER counts computed against different UniProt releases or proteome sets are not strictly comparable - always carry the recorded release forward.

Maps to `lifesci seq.fetch`. Parameters are the input schema in [commands.md](commands.md#seqfetch--fetch-uniprot-sequences-entries-and-proteomes).

## `lifesci_seq_search`

Detect homologs and build a presence/absence matrix with per-cell evidence. Modes: 'phmmer' runs local pyhmmer phmmer (compositional-bias filter OFF by design; reciprocal best-hit checks) and can add a Pfam/HMM domain scan that curates cells; 'hmmsearch' scans profile HMMs against proteomes and curates them with caller-supplied factor signatures (unmapped models become factors named by model id); 'evidence' replays recorded per-model Pfam hit rows (pfam_hits.tsv format), optional sequence rows and reciprocal-check rows offline and curates a presence matrix with evidence strings. Factors and their models are declared via factor_queries / factor_models. Mandatory caveats are always reported: absent means 'no detectable homolog' not proof of absence; E-values depend on database size and are not comparable across proteomes/databases; single-sequence search misses very divergent proteins, so domain/family evidence is the arbiter; weak calls are flagged for human review. Reciprocal checks are reported when available; without them sequence-level exclusions are not corroborated.

Maps to `lifesci seq.search`. Parameters are the input schema in [commands.md](commands.md#seqsearch--homolog-search-and-presence-matrix).

## `lifesci_stats`

Statistical association between two numeric columns of a small table. Reports n, Pearson r (with p), Spearman rho (with p), an optional partial correlation controlling for a nuisance variable (control='auto' uses an existing 'core' column; control='core' derives it from core_from, i.e. total_column minus segment_columns), a log-log fit log(y)=a+slope*log(x), per-group correlations, and standardised-residual outliers. group_by='auto' groups identifiers by the prefix before the first '_' or '-'. Always emits CORRELATION_NOT_CAUSATION and PHYLOGENETIC_NON_INDEPENDENCE caveats, plus SMALL_N below the threshold. Row filters: name_prefix, name_suffix, name_contains, name_regex, ids, and per-column value filters.

Maps to `lifesci stats`. Parameters are the input schema in [commands.md](commands.md#stats--correlation-partial-correlation-and-log-log-regression).

## `lifesci_struct_fetch`

Fetch protein structure models/structures reproducibly. AlphaFold DB entries come with their model version and file URLs; when AFDB has no model (common for TrEMBL entries) the command attempts an ESMFold single-sequence prediction and then, if that also fails, reports an explicit 'no model' verdict in unavailable[] with warnings - an unavailable model is NOT evidence of absence. RCSB PDB mmCIF files and EMDB density maps (large; warning) are also supported. Every written file carries a SHA-256 recorded in provenance, and the model version/release is returned for each available structure. Caveats: AlphaFold/ESMFold models are predictions, not experimental structures; ESMFold is limited to short sequences (HTTP 413 for very long proteins) and can time out; per-residue confidence (pLDDT) is not evaluated here.

Maps to `lifesci struct.fetch`. Parameters are the input schema in [commands.md](commands.md#structfetch--fetch-structure-models).

## `lifesci_struct_search`

Structural-similarity search with explicit interpretation limits. Modes: 'panel' runs the local Foldseek binary on a controlled panel and calibrates the background with negative controls (unrelated proteins of the same fold class); only scores above control_max_tm + margin are interpretable. 'web' runs the Foldseek web API (AFDB50/AFDB-SwissProt/PDB100; only multipart PDB upload works). 'from_table' replays recorded hit tables offline and optionally checks caller-supplied sanity anchors (expected_anchors), warning with SANITY_ANCHOR_MISSING when one is not recovered. Always reported: TM-scores are query-normalised (fragment/short queries score higher), result lists are capped (AFDB50 top ~1000 per database), and E-values are database-size dependent, so E-values must never be compared across databases. Structural similarity supports 'closest structural neighbour' but is not automatic orthology; use reciprocal searches and domain/family evidence as the arbiter.

Maps to `lifesci struct.search`. Parameters are the input schema in [commands.md](commands.md#structsearch--foldseek-structural-search).

## `lifesci_struct_superpose`

Structural comparison with two modes. kabsch: superpose structures on shared alignment columns (from colmap residue→column maps), optionally excluding spans such as a variable region; returns per-structure rotation/translation into the reference frame, core RMSD and local RMSD near the focus spans, plus absent columns. Warns FEW_CORE_COLUMNS and raises NO_SHARED_COLUMNS when too few columns are shared. foldseek: pairwise structural alignment via the local Foldseek binary, reporting TM/LDDT/e-value/coverage per pair. Always treats TM as query-length normalised and E-values as database-size dependent: run reciprocal searches and never compare E-values across databases. Trimmed structures are valid inputs; use the preserved residue numbering and colmap.

Maps to `lifesci struct.superpose`. Parameters are the input schema in [commands.md](commands.md#structsuperpose--superpose-structures-kabsch-on-alignment-columns-or-foldseek).

