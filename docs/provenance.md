# Provenance, cache, and reproducing a result

Every number the plugin returns must be re-derivable from recorded raw payloads plus the recorded
request parameters. This document describes where that information lives and how to audit it.

## 1. The command envelope

Every CLI call and every agent tool call returns one JSON envelope
(`schemas/envelope.schema.json`):

```json
{
  "ok": true,
  "command": "seq.fetch",
  "version": "0.2.0",
  "schema_version": 1,
  "started_at": "2026-11-05T17:00:00Z",
  "finished_at": "2026-11-05T17:00:01Z",
  "duration_s": 0.84,
  "offline": false,
  "dry_run": false,
  "cache_dir": "/work/.lifesci-cache",
  "data": { "…command payload…" },
  "warnings": [
    {"code": "HTTP_TRANSIENT", "message": "…", "severity": "warning", "context": {…}}
  ],
  "human_review": [
    {"code": "SINGLE_WEAK_LINE_EVIDENCE", "message": "…", "severity": "required"}
  ],
  "provenance": [
    {"kind": "http", "request_id": "…", "method": "GET", "url": "…", "status": 200,
     "sha256": "…", "bytes": 1234, "cache": "store", "database": "uniprot",
     "release": "2026_04", "retries": 0, "elapsed_ms": 210.4, "time": "…"},
    {"kind": "command", "command": "seq.fetch", "ok": true, "args": {…}, "…": "…"}
  ]
}
```

Warnings are part of the scientific result: E-value comparability, TM-score normalisation,
top-hit caps, low-identity mappings, absence-not-proof wording, and single-weak-line flags are
attached automatically where they apply. **Do not strip them from a report.**

## 2. Cache layout

```
<cache>/cache.json                                format marker
<cache>/v1/objects/<aa>/<key>.body                 raw payload bytes (verbatim, possibly gzipped)
<cache>/v1/objects/<aa>/<key>.meta.json            status, headers, release, sha256, ttl, stored_at
<cache>/provenance.jsonl                          append-only JSONL provenance log
<cache>/releases.json                             observed database releases (pinned)
<cache>/datasets.json                             registered read-only local datasets
<cache>/blobs/<namespace>/<name>                  derived blobs (HMM models, etc.)
<cache>/matplotlib/                               MPLCONFIGDIR (keeps matplotlib out of $HOME)
```

The key is `sha256(canonical_json({format, method, url, params, accept/content-type, body_sha256}))`.
Two requests with identical semantics share one entry; changing any parameter produces a new key.
On read the body SHA-256 is verified by default; a mismatch raises `CACHE_CORRUPT` (online: the
entry is discarded with a warning and re-fetched; offline: the command fails).

TTL defaults to 30 days; entries flagged immutable never expire. `--cache-ttl 0`
forces a refetch (online). `lifesci_cache` exposes stats/list/verify/prune/clear; clearing
requires `confirm: true`.

## 3. Auditing a result

1. Read the envelope's `provenance[]`. Each HTTP entry has `url`, `params`, `status`, `sha256`,
   `bytes`, `database`, `release`, `retries`, `cache` (`hit`/`store`/`disabled`).
2. Locate the payload: run the same request through `lifesci_http` with the same parameters and
   `offline: true`, or compute the key with
   `python -c "from lifesci.cache import cache_key; print(cache_key('GET', url, params=...))"`.
3. Verify `sha256sum <cache>/v1/objects/<aa>/<key>.body` equals the recorded SHA-256.
4. Re-run the command with the same input under `--offline` — it must reproduce the same `data`
   modulo provenance timestamps. The offline test suite does exactly this for its scenarios.
5. `releases.json` records the database release observed at retrieval time. Reports should cite
   the release string recorded there alongside the result.

## 4. Reproducing a result offline

Offline replay works from the content-addressed cache alone: once a command has run online, every
raw payload it needed is stored under its request key, so re-running the same command with
`--offline` reproduces the same `data` (modulo provenance timestamps) without touching the
network. This is how the offline test suite and archived analyses are re-run.

1. Run the command online once with an explicit `--cache-dir` that you keep with the analysis.
2. Re-run the identical command with `--offline`; it must reproduce the same `data`.
3. A miss is a typed `OFFLINE_CACHE_MISS` that names the missing cache key, so you can run that
   one request online to fill it and retry offline.
4. Cache directories are portable: copy the directory to another machine and replay there — in
   offline mode the tool layer reads the cache and never opens a connection.
5. Keep `provenance.jsonl` and `releases.json` next to the cache: they record what was received
   and which database release it came from, so a reviewer can re-derive the numbers.

Replay is only identical for an identical request identity: URL, query parameters and relevant
headers are part of the cache key, so changing any of them is a different entry and will miss
offline until it has been fetched once online.

## 5. Log retention and privacy

`provenance.jsonl` grows by one line per request plus one per command; it is plain JSONL so it can
be shipped with a publication or analysis record. It contains public scientific payload hashes and
request metadata only; secrets are redacted and bodies are not logged. Trim with `lifesci_cache
action=prune` (expired entries) or delete the log file if an analysis no longer needs it — deleting
the log does not delete cached payloads.
