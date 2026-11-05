# Generic golden suite (synthetic data, offline)

This directory contains the release's end-to-end Python tests. Each test drives
a real command through `lifesci.cli.run_command` — the same entry point the CLI
and the harness bridge use — and validates the returned payload against the
shipped JSON schema in `schemas/`.

Unlike a fixture-replay suite, **nothing here depends on recorded research
payloads**:

* all inputs are small synthetic tables/sequences/structures written to pytest's
  `tmp_path`;
* no network call is made (the suite runs under `-m "not network"`);
* no cache is pre-seeded — a fresh hermetic context is created per test;
* assertions cover *behaviour and contract shape*, not project-specific results:
  statistics are checked against values computed from the synthetic data, and
  evidence curation is checked against caller-supplied factor signatures.

## Files

| File | Covers |
|---|---|
| `conftest.py` | `golden_ctx` fixture (hermetic context under `tmp_path`) |
| `golden_helpers.py` | `run_golden` (run + payload-schema validation), warning helpers, input writers, tiny-gemmi-CIF builder |
| `test_generic_stats.py` | Pearson/Spearman, explicit `core_from` partial correlation, log-log fit, `group_by=auto` prefix grouping, mandatory caveats, typed control errors |
| `test_generic_report.py` | evidence-graded Markdown report (DOI/PMID/URL citations), provenance table, refusal of ungraded/uncited claims |
| `test_generic_align_map.py` | tiny MSA + queries: feature spans, identity/coverage, low-identity flag, `FEATURE_SPAN_UNSTABLE`, `reference_row=auto` |
| `test_generic_contacts.py` | gemmi-built tiny mmCIF: residue selections, minimum distance, missing-feature reporting, determinism |
| `test_generic_seq_search.py` | evidence-mode inline cells/rows: present/weak/absent matrix, unsearched factors omitted, expected-matrix reproduction |
| `test_generic_domains.py` | recorded domain-evidence rows: present/weak/absent verdicts, absence honesty, cross-database-size warning |
| `test_generic_figures.py` | inline rows → PNG plus recorded SHA-256, explicit x/y requirement, histogram kind |

## Running

From the repository root:

```bash
PYTHONPATH=python .venv/bin/python -m pytest python/tests/golden -q -m "not network"
```

`LIFESCI_STRICT_SCHEMAS=1` is not required: `run_golden` validates every payload
against `schemas/<command>.schema.json` explicitly. The shared session guards in
`python/tests/conftest.py` keep the suite deterministic (no network, fake clock,
`MPLCONFIGDIR` under a temp directory).

## Scientific framing

These are contract/behaviour tests. Passing them does **not** validate any
biological conclusion: absence of a detectable hit is never proof of absence,
correlations are not causation, and E-values are database-size dependent. The
commands themselves attach those caveats; the tests assert that the caveats and
warnings are actually emitted.
