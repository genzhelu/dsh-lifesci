# Portal reference — endpoints, quirks, fallbacks, verification status

This document is the operational reference for the public portals behind the `lifesci` adapters
(`python/lifesci/adapters/`). It records what the endpoints do, how they fail, which fallback the
adapter implements, and whether the described behaviour was exercised against the live service or
is written from the public API documentation.

Everything goes through `lifesci.http` (the single retrying, caching, rate-limited,
provenance-logged primitive), so every statement about a failure mode is also a statement about
the adapter's error/warning contract.

## Verification legend

| Status | Meaning |
|---|---|
| **live-verified** | A real request was made against the live service during development, and the observed status/headers/body shape match this document. |
| **live-verified (failure observed)** | The endpoint was probed and failed; the failure is documented here and the adapter has an explicit fallback or unavailable verdict. |
| **offline-verified** | The parsing/behaviour was exercised through the real cache/offline code path during development, with no network. |
| **design-only, unverified** | Described from the portal's public API documentation; not re-verified against the live service. Treat the shape as needing confirmation before relying on new fields. |

Verification status is a development-time snapshot: public services change. Re-check any portal
with `lifesci doctor` plus a small live call before depending on a new field.

## Cross-cutting rules that apply to every portal

1. **All HTTP goes through `lifesci.http`.** No adapter calls `urllib`, `socket` or `curl`
   directly. That gives every portal cache + TTL + offline replay, retries with exponential
   backoff, per-host token buckets, `Retry-After` handling and JSONL/`provenance[]` logging.
2. **Database releases are pinned where the portal exposes them** (UniProt
   `x-uniprot-release`, AlphaFold `latestVersion`, OMA `/api/version/`, OrthoDB
   `/orthodb_release_id`). Where a payload cannot expose a release (InterPro entry payloads,
   RCSB files, ESMFold) the adapter says so with a warning instead of inventing one:
   `DATABASE_RELEASE_UNKNOWN`, `INTERPRO_RELEASE_UNKNOWN`, etc.
3. **Documented quirks become machine-readable warnings**, never silent source switches:
   fallback used, pagination/top-hit caps, release unknown, E-value database dependence, TM
   query-normalisation.
4. **Failures are typed** (`lifesci.errors` with stable `code`, `retriable`, `details`,
   `fallbacks`), so the agent can branch on `code` and follow the suggested alternate source.
5. **Deterministic data**: no wall-clock timestamps in returned `data`; lists that are not
   intrinsically ordered are sorted; floats are rounded only where display-bound. Retrieval
   time/status/sha256 live in provenance.
6. **Offline replay requires an identical request identity**: the adapter must issue exactly the
   method + URL + query params + relevant headers that were cached (the key is
   `sha256(canonical_json({format, method, url, params, accept/content-type, body_sha256}))` — see
   `lifesci.cache.cache_key`). See the [offline replay contract](#14-offline-replay-contract).

---

## 1. UniProt REST / FTP

Adapter: `lifesci.adapters.uniprot` · database label `uniprot` · release header
`x-uniprot-release` (the value observed at retrieval time is recorded in the release registry).

| Purpose | Request |
|---|---|
| sequence | `GET https://rest.uniprot.org/uniprotkb/{acc}.fasta` |
| entry metadata | `GET https://rest.uniprot.org/uniprotkb/{acc}.json` |
| bulk search | `GET https://rest.uniprot.org/uniprotkb/search?query=…&format=tsv&fields=…&size=…` |
| proteome discovery | `GET https://rest.uniprot.org/proteomes/search?query=organism_id:<taxid>&format=json` |
| proteome by id | `GET https://rest.uniprot.org/proteomes/<PROTID>` |
| proteome stream | `GET https://rest.uniprot.org/uniprotkb/stream?query=proteome:<PROTID>&format=fasta&compressed=true` |
| FTP fallback | `https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/reference_proteomes/Eukaryota/<PROTID>/<PROTID>_<TAXID>.fasta.gz` |

Status: **live-verified** (FASTA/entry/TSV/proteomes/FTP), **live-verified (failure observed)**
for the stream endpoint.

Worked behaviour:

* `…/P69905.fasta` → `200`, `content-type: text/plain;format=fasta`, `x-uniprot-release`
  header; header
  `>sp|P69905|HBA_HUMAN Hemoglobin subunit alpha OS=Homo sapiens OX=9606 GN=HBA1 PE=1 SV=2`.
  The adapter parses the accession, entry name, organism and sequence (ungapped length 142) and
  returns `release`.
* `…/P69905.json` → `200`; normalised into `parsed` (`primaryAccession`, `entryType`,
  `reviewed`, `protein_name`, `genes`, `organism{taxon_id, scientific_name}`, `sequence{length,
  mol_weight, checksum}`, cross-references, features, comments, keywords, references with
  PMID/DOI). The raw JSON is kept in `entry` so nothing is lost.
* `…/search?format=tsv` → `200` with an `x-total-results` header; pagination uses the
  `Link: <…cursor=…&size=…>; rel="next"` response header. Columns come back in the requested
  field order, so rows can be keyed by field id.
* `…/proteomes/search?query=organism_id:<taxid>` → `200`. The adapter prefers the proteome whose
  `proteomeType` contains *Reference*, then the largest `proteinCount`, and warns
  `UNIPROT_MULTIPLE_PROTEOMES` when it had to choose.
* FTP fallback: the gzip body is **served without `Content-Encoding: gzip`**, so adapters must
  detect gzip magic themselves (`_common.decode_payload` does).

Failure modes / fallbacks:

* **`/uniprotkb/stream` is the fragile endpoint.** During probing it could stall with no response,
  while the same release was served from FTP. The adapter therefore tries the stream first
  (context retry budget); on 429/5xx/transport failure **or an empty payload** it falls back to
  the FTP reference-proteome FASTA and records `UNIPROT_STREAM_FALLBACK_FTP` (message names the
  REST error and the FTP URL). 400/401/403 are not masked.
* **FTP 404** means the proteome is not a *reference* proteome (only those are mirrored) or the
  `<PROTID>_<TAXID>` filename does not match. The adapter raises
  `NotFoundError(code="UNIPROT_PROTEOME_NOT_FOUND")` with explicit fallbacks: resolve/confirm the
  reference proteome, retry the REST stream with backoff, use a different proteome or a local
  dataset.
* The FTP path uses `current_release`, i.e. a moving target; because the gz file carries no
  release header the adapter warns `DATABASE_RELEASE_UNKNOWN` for FTP fallback payloads and pins
  the file SHA-256 instead.
* If the caller omits `taxid` and the fallback is needed, the taxid is resolved from
  `GET /proteomes/<PROTID>` (`taxonomy.taxonId`); if that fails the adapter raises
  `UNIPROT_TAXID_REQUIRED` instead of guessing a filename.
* `fetch_fasta`/`fetch_entry` translate portal 404 into `UNIPROT_ACCESSION_NOT_FOUND` (verify the
  accession; the entry may be merged or deleted). Secondary-accession resolution is surfaced as
  `UNIPROT_ACCESSION_RESOLVED`.

## 2. NCBI E-utilities

Adapter: `lifesci.adapters.ncbi` · database label `ncbi`.

| Purpose | Request |
|---|---|
| search | `GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=…&term=…&retmode=json&retmax=N` |
| summaries | `GET …/esummary.fcgi?db=…&id=…&retmode=json` |
| FASTA | `GET …/efetch.fcgi?db=nuccore&id=…&rettype=fasta&retmode=text` |
| text | `GET …/efetch.fcgi?db=…&id=…&rettype=…&retmode=text|xml` |
| taxonomy | `GET …/esummary.fcgi?db=taxonomy&id=…&retmode=json` (+ XML lineage on request) |

Status: **live-verified** (esearch/esummary/efetch, taxonomy via esummary), **live-verified
(failure observed)** for the taxonomy-JSON quirk.

Worked behaviour:

* esearch `db=pubmed,term=<query>` → `200`,
  `{"esearchresult":{"count":"…","idlist":[…],"querytranslation":"…"}}`; the adapter records the
  `tool=lifesci` parameter (and `email`/`api_key` when configured), retries body-level errors and
  HTTP 500 HTML pages.
* esummary → `200`, `{"result":{"uids":[…],"<uid>":{…}}}`.
* **Quirk: `efetch.fcgi?db=taxonomy&id=9606,559292&retmode=json` returns HTTP 200 with plain
  taxid lines, not JSON** (the endpoint echoes the ids). The adapter therefore reads taxonomy
  from the working `esummary.fcgi db=taxonomy retmode=json` endpoint, whose records carry
  `status`, `rank`, `division`, `scientificname`, `commonname`, `taxid`. Lineage is opt-in
  (`include_lineage=True`) via `efetch … retmode=xml` (`TaxaSet/Taxon/LineageEx`).

Failure modes / fallbacks: E-utilities is flaky by design — HTTP 500 HTML pages
("eutils201 WWW Error 500") and 200-body JSON errors (`{"error":"error forwarding request …",
"type":"ip"}`). `lifesci.http` detects the body-level error via
`error_detector_from_keys("error")` and retries with backoff; on exhaustion the adapter re-raises
the `NCBI_<status>` error with fallbacks to **Europe PMC search** and the **PubMed web interface /
MEDLINE page**. **NCBI BLAST is intentionally not wrapped**: its web queue is too slow to be
usable; homology search uses local pyhmmer (`lifesci.lib.hmmer`).

## 3. Europe PMC

Adapter: `lifesci.adapters.europepmc` · database label `europe_pmc`.

| Purpose | Request |
|---|---|
| literature search | `GET https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=…&format=json&pageSize=…&resultType=…&cursorMark=…` |
| full text | `GET https://www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/fullTextXML` |

Status: **live-verified** (search + shape); full text is **design-only, unverified**.

Worked behaviour: a search returns
`{"version":…,"hitCount":…,"nextCursorMark":…,"resultList":{"result":[{"id","source","pmcid",
"doi","title","authorString","journalTitle","pubYear",…}]}}`. Note the **double nesting**
`resultList.result` (a parse bug in an early implementation produced all-`null` hits; fixed and
re-verified). A non-null `abstractText` requires `resultType=core`.

Failure modes / fallbacks: intermittent HTTP 503 is retried with backoff; `nextCursorMark` drives
pagination and `hitCount` is the full-query count, so partial pages raise a truncation warning. A
missing full text is a typed not-found with fallbacks (NCBI PMC OA, publisher page, abstract
instead).

## 4. InterPro / Pfam

Adapter: `lifesci.adapters.interpro` · database label `interpro` · release metadata is read from
the InterPro API root (observed releases at development time: InterPro 111.0, Pfam 39.0,
PANTHER 19.0).

| Purpose | Request |
|---|---|
| all annotations of a protein | `GET https://www.ebi.ac.uk/interpro/api/entry/all/protein/uniprot/{acc}/?page_size=50` |
| entry definition | `GET https://www.ebi.ac.uk/interpro/api/entry/{database}/{entry_id}/` |
| Pfam matches of one protein | `GET https://www.ebi.ac.uk/interpro/api/entry/pfam/{PF}/protein/uniprot/{acc}/?page_size=N` |
| HMM model | `GET https://www.ebi.ac.uk/interpro/wwwapi/entry/{database}/{entry_id}/?annotation=hmm` |
| release pinning | `GET https://www.ebi.ac.uk/interpro/api/` (`databases.<name>.version`) |

Status: **live-verified** (all four endpoints + release metadata), **offline-verified** for
parsing through the cache.

Worked behaviour:

* `entry/all/protein/uniprot/P69905/?page_size=50` → `200`,
  `{"count":…,"next":…,"results":[{"metadata":{"accession":"<IPR accession>",
  "name":"Globin","source_database":"interpro","type":"domain","member_databases":{"pfam":
  {"PF00042":"Globin"}},…},"proteins":[{"accession":"p69905","protein_length":142,
  "source_database":"reviewed","in_alphafold":true,"entry_protein_locations":[{"fragments":[
  {"start":…,"end":…,"dc-status":"CONTINUOUS"}],"representative":true,"model":"PF00042",
  "score":…}]}]},…]}`. The adapter returns one record per entry with `source_database`,
  `entry_accession`, `entry_name`, `entry_type`, `integrated`, `member_databases`, `go_terms`
  and `locations` (`start/end/evalue/score/model/representative/fragments`).
* Pagination: follow the JSON `next` URL; `count` is the total; a capped list is flagged
  (`truncated` + `TOP_HIT_LIST_CAPPED`).
* **Score-vs-E-value quirk:** InterPro exposes a single generic `score` field per match; for
  Pfam/PANTHER/PRINTS it *is* the match E-value. The adapter puts the raw value in both `evalue`
  and `score`; the command layer disambiguates with `_looks_like_evalue` and records
  `score_source`. When no location is returned for a requested Pfam model, the domains command
  reports an explicit domain-absent verdict.
* Entry metadata: Pfam `metadata.name` is an object `{"name":…,"short":…}` while integrated
  entries use a string; `description` is a list of HTML fragments and is rendered to plain text
  (raw HTML preserved in `description_html`).
* `?annotation=hmm` → `200`, `content-type: application/gzip`; the body decompresses to a
  **plain `HMMER3/f` text model** — *not* a tar archive (a tar container is still tolerated and
  warned `INTERPRO_HMM_TAR_CONTAINER`). Models are cached under `<cache>/models/pfam_<id>.hmm[.gz]`.
* **Release is not in entry payloads.** `interpro.release_info(ctx)` performs the one extra
  `GET /interpro/api/` request to pin InterPro/Pfam/PANTHER/UniProt versions on demand;
  annotation calls never make that implicit request, so offline replay of a recorded entry payload
  stays a single-request operation and returns `release: None` with an
  `INTERPRO_RELEASE_UNKNOWN` info warning.

Failure modes / fallbacks: HTTP 404 → `INTERPRO_*_NOT_FOUND` (verify the accession; obsolete
entries disappear between releases); 5xx/503 retried by `lifesci.http`. HMM payloads that are not
HMMER3 raise `DataError(code="INTERPRO_HMM_INVALID")` rather than feeding a corrupt model to
pyhmmer.

## 5. AlphaFold DB

Adapter: `lifesci.adapters.alphafold` · database label `alphafold` · release pinned as the model
version (`latestVersion`).

Endpoint: `GET https://alphafold.ebi.ac.uk/api/prediction/{ACC}`.

Status: **live-verified** (available and missing-model cases), **offline-verified** for parsing.

Worked behaviour: a `200` response is a **JSON list**; the first record contains
`modelEntityId: "AF-P69905-F1"`, `latestVersion`, `sequenceStart`, `sequenceEnd`,
`pdbUrl`/`cifUrl`/`bcifUrl`, `uniprotAccession`, `gene`, `organismScientificName`, `taxId`,
`sequenceChecksum`, and pLDDT fractions. The adapter returns available models with `entry_id`,
`model_version`, URLs, UniProt interval, and records the model version in the release registry
(the API has no release header).

Failure mode: **HTTP 404 with a tiny `{}` body** for entries without a prediction. A 404 is
terminal and **not retried**; `prediction()` returns `available: false` with a warning (never an
error), and the *command* decides whether to try ESMFold (warning
`AFDB_UNAVAILABLE_ESMFOLD_FALLBACK`) and finally reports an explicit unavailable verdict. An
advertised model URL that later 404s is reported as available-false with
`ALPHAFOLD_MODEL_DOWNLOAD_404`; downloaded bytes are validated as PDB/mmCIF before being written
and SHA-256-pinned.

## 6. ESMFold (ESM Atlas)

Adapter: `lifesci.adapters.esmfold` · database label `esmatlas`.

Endpoint: `POST https://api.esmatlas.com/foldSequence/v1/pdb/`, `Content-Type: text/plain`,
body = raw sequence → PDB text.

Status: **live-verified (failure observed)** — the service answered HTTP 504 for even short
sequences during probing, so the *success path and the documented 413 long-sequence response are
design-only, unverified*. The adapter handles the documented quirks as unavailable verdicts:

* HTTP **413** for long sequences → not retried, `available: false`, warning
  `ESMFOLD_SEQUENCE_TOO_LONG`;
* HTTP **504** when busy → retried exactly once (`idempotent=True, retries=1`), then
  `available: false`, warning `ESMFOLD_UNAVAILABLE`;
* any exhausted service failure → explicit unavailable (never a crash, never a fabricated model);
  sequences longer than 1400 aa additionally warn `ESMFOLD_LONG_SEQUENCE` before submission.

## 7. Foldseek (local binary + web API)

### 7.1 Local static binary

Adapter functions: `version`, `local_search`, `create_db` (database label `foldseek`). Binary
resolution order: explicit `binary=` → `$LIFESCI_FOLDSEEK` → `tools/bin/foldseek` → `PATH`.
Installation is a separate checksum-pinned script (`scripts/install_foldseek.sh`); there is
**no silent auto-update**.

Commands used:
`foldseek version`;
`foldseek createdb <dir> <db>`;
`foldseek easy-search <query> <target> <out.m8> <tmp> --format-output
"query,target,alntmscore,qtmscore,ttmscore,lddt,evalue,qcov,tcov,alnlen" -e 100
--threads N`.

Status: **live-verified** by the command path — a panel search created a database and ran
`easy-search` against real AlphaFold DB models, with self-hits at 1.0 and calibrated negative
controls. The adapter's own m8 parse/format logic is **design-only** here (not re-verified
against a recorded m8 in this pass), while the version/argv plumbing is exercised by the command
path.

Interpretation limits that the command layer warns automatically: local Foldseek **E-values
depend on the target database size** and are not comparable to panel/web values of the same pair;
TM is query-length normalised. Use local panels for controlled comparisons (references +
candidates + negative controls), never for finding unknown homologs.

### 7.2 Foldseek web API (`search.foldseek.com`)

Endpoints: `POST https://search.foldseek.com/api/ticket` (multipart),
`GET /api/ticket/<id>` (status: `PENDING|RUNNING|COMPLETE|ERROR`),
`GET /api/result/<id>/<db_index>` (large JSON).

Status: **live-verified** submission + status polling (a `1UBQ.pdb` multipart upload with
`mode=tmalign`, `database[]=afdb50` returned a ticket and polled to `COMPLETE`). The result JSON
normalisation is **design-only, unverified** in this pass; the documented shape is
`results[0].alignments[0]` — a list of hit dicts with `target`, `taxName`, `taxId`, `prob`,
`eval`, `score`, `alnLength`, `qStartPos/qEndPos/dbStartPos/dbEndPos`, `qLen`, `dbLen`,
sequences — and the adapter implements exactly that (plus a shape-drift fallback).

Documented quirks implemented:

* **Only multipart PDB upload works.** JSON-body submission returns HTTP 400
  (`JobRequest.Job.Database/Mode required`); FASTA/sequence submissions create a ticket but the
  job ends `ERROR`. The adapter never tries JSON/FASTA and says so in the 400 error's fallbacks.
* Tickets are polled with `cache=False` (a cached `PENDING` would be a lie) until `COMPLETE`,
  `ERROR`, or `max_wait`; timeout raises a retriable `FOLDSEEK_TIMEOUT` with the ticket so the
  agent can poll later.
* Interpretation: server `score` is query-normalised **TM × 100** in `tmalign` mode
  (`tm_score = score/100`; raw score retained), `prob` is Foldseek's homology probability, and
  `eval` depends on database size — the adapter emits `EVALUE_NOT_COMPARABLE_ACROSS_DATABASES`
  and `TM_SCORE_QUERY_NORMALISED` warnings next to normalized hits. Result lists are
  server-capped, so absence from them is not proof of absence.
* Never compare E-values across databases: the same structural pair can receive a very small
  E-value against a compact local panel and a large one against AFDB50.

## 8. RCSB PDB / EMDB

Adapter: `lifesci.adapters.pdb` · database labels `rcsb_pdb`, `emdb`.

| Purpose | Request |
|---|---|
| structure | `GET https://files.rcsb.org/download/<PDB>.cif` (or `.pdb`) |
| EMDB map | `GET https://ftp.ebi.ac.uk/pub/databases/emdb/structures/EMD-<id>/map/emd_<id>.map.gz` |
| PDBe alternate (fallback) | `https://www.ebi.ac.uk/pdbe/entry-files/download/<id>.cif` |

Status: **live-verified** (structure downloads; EMDB HEAD requests), **live-verified (failure
observed)** for the zero-padding trap below.

Worked behaviour / quirks:

* RCSB files are plain mmCIF/PDB (`data_1UBQ` / `HEADER …`). Downloads are SHA-256-pinned and
  validated before use; 404 → `PDB_NOT_FOUND` with PDBe/accession fallbacks. Files have no
  release header — the file hash is the pin.
* **EMDB accessions are NOT zero-padded in the file layout** (verified: `EMD-1000` →
  `.../EMD-1000/map/emd_1000.map.gz` is `200`, the padded `emd_01000.map.gz` is `404`, while
  `EMD-10000` → `emd_10000.map.gz` is `200`). An early implementation padded to five digits and
  would have missed every map below `EMD-10000`; fixed. Map files are large (tens of MB) and are
  warned/stored gzipped; decompress before gemmi.
* `structure_entities` delegates to `lifesci.lib.structures` (gemmi); the modelled chain list is
  what the model actually contains, not the entity table (some protein/RNA chains are
  unmodelled). `gemmi.Model.__contains__` tests chains not names, and gemmi has no
  `Structure.write` (use `st.make_mmcif_document().write_file`) — both handled in the library.

## 9. SGD (Saccharomyces Genome Database)

Adapter: `lifesci.adapters.sgd` · database label `sgd`.

Endpoint (live-verified):
`GET https://www.yeastgenome.org/backend/locus/<ORF-or-gene>/interaction_details`.

Status: **live-verified**, with a documented endpoint deviation.

* The older `/locus/<ORF>/interaction` endpoint returns **HTTP 404 with a `null` body for tested
  loci**. The backend serves the curated records at `/locus/<ORF>/interaction_details` (a bare
  JSON list). The adapter uses the working path and normalises the nested objects:
  `locus1`/`locus2` (each with `format_name` ORF + `display_name` gene),
  `experiment.display_name`, `interaction_type`, `annotation_type`, `source.display_name`
  (e.g. BioGRID), `reference.pubmed_id` (an **integer** — must be stringified).
* Responses are slow (seconds); budget command timeouts accordingly.
* Curated interaction evidence is **not** proof of complex stoichiometry, so the payload carries
  that caveat.
* 404 → `SGD_LOCUS_NOT_FOUND` with BioGRID/IntAct/ComplexPortal fallbacks.

## 10. OMA (Orthologous MAtrix)

Adapter: `lifesci.adapters.oma` · database label `oma`.

Endpoints: `GET https://omabrowser.org/api/protein/<acc>/` and
`GET https://omabrowser.org/api/version/`.

Status: **live-verified**.

* The protein payload is **flat**, not nested under `protein`:
  `{"entry_nr":…,"omaid":"HUMAN…","canonicalid":"HBA_HUMAN","sequence_md5":…,
  "sequence_length":142,"species":{"code":"HUMAN","taxon_id":9606,"species":"Homo sapiens"},
  "oma_group":…,"oma_hog_id":"HOG:…","roothog_id":…,"hog_levels":[…],"description":…,
  "oma_group_url":…,"oma_hog_members":…,"orthologs":…,"sequence":…}`.
  `oma_hog` comes from `oma_hog_id` and keeps the `HOG:` prefix; the members endpoint is a URL,
  not an inline list (`hog_members: null` unless the caller fetches it).
* Release: `/api/version/` returns `{"oma_version":…,"api_version":…}`; the data release is
  fetched best-effort, recorded in the release registry and returned as `release` (unavailable
  offline → warning, result still returned).
* Unknown accessions return non-200 error bodies → `OMA_NOT_FOUND` with OMA browser / OrthoDB /
  PANTHER fallbacks.
* Interpretation: OMA groups are strict orthologue sets, HOGs are hierarchical; neither is a
  curated one-to-one orthology statement.

## 11. OrthoDB

Adapter: `lifesci.adapters.orthodb` · database label `orthodb` · release observed **v13.0**,
API version v13.

Endpoints: `GET https://data.orthodb.org/v13/group?id=<og_id>`,
`GET https://data.orthodb.org/v13/search?query=…&take=N&level=<taxid>`,
`GET https://data.orthodb.org/v13/orthodb_release_id`.

Status: **live-verified**, with a **documented endpoint change**.

* **Older `/v102/ogroup/<id>` and `/v11/ogroup/<id>` endpoints are dead** (HTTP 404 `{}`). The
  current API is v13. The v13 `/group?id=` response is **wrapped**:
  `{"data":{"id":"<og_id>","name":…,"tax_id":2759,"public_id":…,"level_name":"Eukaryota",
  "interpro_domains":[…],"phyletic_profile":{"gene_count":…,"present_in":…},
  "evolutionary_rate":…,"gene_architecture":…},"status":"ok"}`. `n_genes` is read from
  `phyletic_profile.gene_count` when there is no direct count.
* OrthoDB documents that **OG ids are not inherited across releases** (an id can reappear with
  unrelated content). `orthogroup()` therefore falls back to `search?query=<og_id>` and builds
  the record from the `bigdata` metadata (`{"id","d2t","name","public_id","relevance",
  "gene_count","level_name","present_in"}`) if the group id is absent, warning
  `ORTHODB_OGROUP_FALLBACK_SEARCH`; unknown ids raise `ORTHODB_OG_NOT_FOUND` with advice to
  re-resolve by gene name / UniProt accession.
* `/search` returns `data` as a list of og ids plus parallel `bigdata` metadata; results are
  ordered by relevance, capped by `take` and `TOP_HIT_LIST_CAPPED` is warned when the total
  `count` exceeds the page. Release comes from `/orthodb_release_id` and is registry-recorded;
  if unreachable the adapter returns an inferred release plus `ORTHODB_RELEASE_UNKNOWN`.
* Interpretation: orthogroups are inferred homology groups; `level_taxid` constrains what the
  group means.

## 12. Local read-only datasets (no network)

Adapter: `lifesci.adapters.local_dataset` · command `lifesci.datasets`.

Reference data that is too large or too specific to fetch (alignments, curated tables,
structure/model collections) can be registered as a **read-only, checksum-pinned dataset** in
`ctx.datasets_file` (default `<cache>/datasets.json`):
`{"version":1,"datasets":{name:{kind,path,sha256,bytes,description,registered_at,read_only:true,
…}}}`. Supported kinds: `fasta`, `tsv`, `json`, `msa`, `structure`, `table`, `hmm` (files) and
`structure_dir`, `processed_dir`, `generic_dir`, `hmm_dir` (directories hashed file-by-file into
a sorted manifest).

Status: **offline-verified** (no external service).

* Registration refuses missing paths (`DATASET_PATH_MISSING`) and a file/dir kind mismatch, and
  with `verify=True` rejects payloads that are not a plausible instance of the kind
  (FASTA/TSV/JSON/structure via gemmi/HMMER3).
* Directory datasets store a per-file `files[]` manifest and an aggregate SHA-256;
  `verify_dataset` re-hashes and reports mismatched / missing / extra files.
* `resolve_dataset_file` re-verifies the SHA-256 **on every read** and refuses a modified file
  (`DATASET_CHECKSUM_MISMATCH`) or a path escaping the dataset directory
  (`DATASET_PATH_ESCAPE`); the plugin never writes inside a dataset.
* A small advisory lock (`<registry>.lock`) serialises concurrent registrations, and `--dry-run`
  computes the record without writing.

## 13. Remote SSH execution (opt-in, off by default)

Adapter: `lifesci.adapters.remote` · command `lifesci.remote`.

Status: **design-only, unverified** (no SSH host was contacted from the development environment).
Configuration is read *only* from the environment:

* `LIFESCI_REMOTE_HOSTS` — JSON `{"hosts":{"<alias>":{"host","user","port","key_path?",
  "work_dir?","tool_paths?","allowed_commands":[[…]]}}}`;
* `LIFESCI_REMOTE_CONFIG` — path to a JSON file with the same shape.

Nothing is hard-coded, no credentials are stored in the repository, key paths are **never
returned or logged** (only `key_configured: true/false`), and the configured hosts list exposes
host/user/port/work_dir/n_allowed_commands/tool_paths keys. A real `run` requires **both** an
explicit opt-in (`allow=True`, which the command only passes for `action=run`) and an
`allowed_commands` argv-prefix allow-list (deny-by-default); argv is quoted with `shlex.quote`,
never handed to a caller-supplied shell string, and `ssh -o BatchMode=yes` avoids password hangs.
Provenance records the host **alias**, redacted ssh argv, start/end time and exit code; exit 255
(connection/auth failure) raises `REMOTE_SSH_FAILED`. See [`remote.md`](remote.md) for
configuration examples — the plugin ships no hostnames.

## 14. Offline replay contract

Offline tests and archived analyses seed the content-addressed cache and run the normal
`--offline` code path. For replay to hit, the adapter's request must match the recorded identity
exactly: method, URL, sorted query params, relevant headers (`accept`/`content-type`), and body
SHA-256 when a body is sent. The key is
`sha256(canonical_json({format, method, url, params, accept/content-type, body_sha256}))`.

Representative request identities used by the adapters:

| Adapter call | Method | URL | params | request headers |
|---|---|---|---|---|
| `uniprot.fetch_fasta(acc)` | GET | `https://rest.uniprot.org/uniprotkb/{acc}.fasta` | – | – |
| `interpro.entry_all_for_protein(acc)` | GET | `https://www.ebi.ac.uk/interpro/api/entry/all/protein/uniprot/{acc}/` | `page_size=50` | `accept: application/json` |
| `alphafold.prediction(acc)` | GET | `https://alphafold.ebi.ac.uk/api/prediction/{acc}` | – | `accept: application/json` |

To build a replayable cache, run the command once online with an explicit `--cache-dir` and keep
that directory with the analysis; any later `--offline` run that issues the same requests replays
from it. A miss is reported as `OFFLINE_CACHE_MISS` together with the missing key, so a single
online run can fill the gap. Offline replay reaches "explicit unavailable" rather than silently
succeeding when a *derived* request (e.g. a model-file download URL) was never part of the cache.

## 15. Known endpoint deviations (public portal reality)

| Endpoint | Reality (live-verified) | Adapter behaviour |
|---|---|---|
| SGD `/locus/<ORF>/interaction` | 404 `null` | uses `/interaction_details` |
| OrthoDB `/v102/ogroup/<id>` / `/v11/ogroup/<id>` | 404 (v102 retired; v13 current) | uses `/v13/group?id=` + search fallback |
| NCBI `efetch db=taxonomy retmode=json` | returns plain taxid lines, not JSON | uses `esummary db=taxonomy retmode=json`; XML lineage opt-in |
| EMDB file name | accession digits unpadded (`emd_1000.map.gz`) | no zero padding |
| Europe PMC hits location | `resultList.result` (double nesting) | unwrapped |
| InterPro payload release | not exposed in entry payloads | `release: None` + warning; `interpro.release_info()` to pin |
| ESMFold success | service returned 504 throughout probing | explicit unavailable; 413/504 handled |

Additional notes:

* **NCBI BLAST is deliberately not implemented** (queue too slow); sequence homology uses local
  pyhmmer with `bias_filter=False`.
* **ComplexPortal is optional and not implemented** in this adapter set.
* EggNOG/PANTHER evidence is read from UniProt cross-references / InterPro rather than a
  dedicated mapper call.
* Remote SSH and ESMFold are the only adapters whose *success* path is design-only here; every
  other endpoint above was exercised against the live service. Verification status describes a
  development-time snapshot and should be re-checked against the current service.
