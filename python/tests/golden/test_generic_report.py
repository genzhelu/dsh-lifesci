"""Generic offline golden tests for ``report``.

A report is assembled from evidence-graded claims and written to disk. The
tests use only synthetic claims; they assert the emitted Markdown, the grade
tally and the provenance table, and that the command *refuses* to emit an
ungraded or uncited claim (the evidence-grading contract).
"""

from __future__ import annotations

from pathlib import Path

from lifesci.cli import run_command

from tests.golden.golden_helpers import run_golden, warning_codes

_TITLE = "Synthetic evidence report"
_SUPPLIED_PROVENANCE = {
    "kind": "local_file",
    "path": "synthetic-measurements.tsv",
    "sha256": "a" * 64,
    "bytes": 123,
}


def _sections() -> list[dict]:
    return [
        {
            "heading": "Findings",
            "intro": "Synthetic results used to exercise the report contract.",
            "claims": [
                {
                    "id": "C1",
                    "text": "Synthetic claim one is established.",
                    "grade": "ESTABLISHED",
                    "citations": [{"doi": "10.1000/example", "title": "Example paper"}],
                },
                {
                    "id": "C2",
                    "text": "Synthetic claim two is inferred.",
                    "grade": "INFERRED",
                    "citations": [{"url": "https://example.org/paper"}],
                },
                {
                    "id": "C3",
                    "text": "Synthetic claim three remains unresolved.",
                    "grade": "UNKNOWN",
                    "citations": [{"pmid": "12345678"}],
                    "note": "No experiment resolves this question.",
                },
            ],
        }
    ]


def test_report_writes_graded_markdown_with_provenance(golden_ctx, tmp_path):
    out_path = tmp_path / "out" / "report.md"
    envelope = run_golden(
        golden_ctx,
        "report",
        {
            "title": _TITLE,
            "sections": _sections(),
            "caveats": ["Synthetic caveat: not a biological conclusion."],
            "provenance": [_SUPPLIED_PROVENANCE],
            "generated_at": "2026-01-01T00:00:00Z",
            "out": str(out_path),
        },
        schema="report.schema.json",
    )
    data = envelope["data"]

    assert data["title"] == _TITLE
    assert data["n_claims"] == 3
    assert data["grades"] == {"ESTABLISHED": 1, "INFERRED": 1, "UNKNOWN": 1}
    assert data["caveats"] == ["Synthetic caveat: not a biological conclusion."]
    assert data["generated_at"] == "2026-01-01T00:00:00Z"
    assert data["n_provenance"] >= 1

    markdown_path = Path(data["markdown_path"])
    assert markdown_path == out_path
    assert markdown_path.is_file()
    json_path = Path(data["json_path"])
    assert json_path.is_file()

    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith(f"# {_TITLE}")
    assert "2026-01-01T00:00:00Z" in markdown
    # Every claim carries its grade and at least one citation.
    for grade in ("ESTABLISHED", "INFERRED", "UNKNOWN"):
        assert f"**{grade}**" in markdown
    assert "doi:10.1000/example" in markdown
    assert "https://example.org/paper" in markdown
    assert "PMID:12345678" in markdown
    # Caveats and the supplied provenance record are printed.
    assert "## Caveats" in markdown
    assert "Synthetic caveat: not a biological conclusion." in markdown
    assert "## Provenance" in markdown
    assert "local_file" in markdown
    assert _SUPPLIED_PROVENANCE["path"] in markdown
    assert _SUPPLIED_PROVENANCE["sha256"] in markdown


def test_report_refuses_ungraded_claim(golden_ctx, tmp_path):
    envelope = run_command(
        "report",
        {
            "title": "T",
            "sections": [
                {
                    "heading": "Findings",
                    "claims": [
                        {
                            "text": "A claim without a grade.",
                            "citations": [{"doi": "10.1000/example"}],
                        }
                    ],
                }
            ],
            "out": str(tmp_path / "report.md"),
        },
        ctx=golden_ctx,
        validate=True,
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "EVIDENCE_GRADE_MISSING"
    assert not (tmp_path / "report.md").exists(), "a refused report must not be written"


def test_report_refuses_uncited_claim(golden_ctx, tmp_path):
    envelope = run_command(
        "report",
        {
            "title": "T",
            "sections": [
                {
                    "heading": "Findings",
                    "claims": [{"text": "An established but uncited claim.", "grade": "ESTABLISHED"}],
                }
            ],
            "out": str(tmp_path / "report.md"),
        },
        ctx=golden_ctx,
        validate=True,
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "EVIDENCE_CITATION_MISSING"
    assert not (tmp_path / "report.md").exists()


def test_report_unknown_claim_also_needs_a_citation(golden_ctx, tmp_path):
    envelope = run_command(
        "report",
        {
            "title": "T",
            "sections": [
                {
                    "heading": "Open questions",
                    "claims": [{"text": "An unresolved claim.", "grade": "UNKNOWN"}],
                }
            ],
            "out": str(tmp_path / "report.md"),
        },
        ctx=golden_ctx,
        validate=True,
    )
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "EVIDENCE_CITATION_MISSING"
    assert "UNKNOWN" in envelope["error"]["message"]


def test_report_success_carries_no_evidence_grading_errors(golden_ctx, tmp_path):
    envelope = run_golden(
        golden_ctx,
        "report",
        {
            "title": "Clean report",
            "sections": _sections(),
            "generated_at": "2026-01-01T00:00:00Z",
            "out": str(tmp_path / "clean.md"),
        },
        schema="report.schema.json",
    )
    codes = set(warning_codes(envelope))
    assert "EVIDENCE_GRADE_MISSING" not in codes
    assert "EVIDENCE_CITATION_MISSING" not in codes
