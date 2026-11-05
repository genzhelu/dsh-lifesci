"""Pure-ish science library behind the ``lifesci.*`` commands.

The modules here package proven implementation patterns into reusable, JSON-friendly
functions.  Design rules:

* Functions return plain JSON-serialisable data (dicts/lists/floats/strings).
* Functions are pure unless they need to fetch, run an external tool, or emit
  envelope warnings — only those take a :class:`lifesci.context.Context` as the
  first argument (``ctx``); everything else takes explicit paths/config.
* Heavy optional libraries (numpy/scipy/matplotlib/pyhmmer/Bio/gemmi) are
  imported inside the module that needs them, never from this package's
  ``__init__``, so importing :mod:`lifesci.lib` stays cheap (``doctor`` fast).
* No function prints to stdout.  Diagnostics are warnings recorded on ``ctx``
  or returned in the payload.
* Outputs are deterministic: lists that are not intrinsically ordered are
  sorted, floats are rounded where the value is display-bound, and no
  wall-clock/random values appear in ``data``.

Modules:

``evidence``      presence/absence curation rules and evidence strings
``hmmer``         pyhmmer phmmer/hmmsearch and reciprocal best hits
``alignment``     PairwiseAligner mappings, feature spans, query measurement
``structures``    gemmi parsing, residue selectors, atom collection, mmCIF IO
``superposition`` Kabsch/refinement and column-based multi-structure superposition
``contacts``      KD-tree atom contacts, percentile positions, bridge detection
``stats``         correlations, partial correlation, log-log regression
``figures``       matplotlib (Agg) figure generation
``pymol_session`` PyMOL ``.pml`` generation and optional headless validation
``report``        evidence-graded Markdown reports with provenance tables
"""

from __future__ import annotations

__all__ = [
    "alignment",
    "contacts",
    "evidence",
    "figures",
    "hmmer",
    "pymol_session",
    "report",
    "stats",
    "structures",
    "superposition",
]
