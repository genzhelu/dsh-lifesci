/* AUTO-GENERATED FILE — do not edit by hand.
 * Regenerate with: python -m lifesci.toolspec --out src/tool-specs.ts
 * Source of truth: python/lifesci/commands/*.py
 */
export const TOOL_SPECS = {
  "package": "dsh-lifesci",
  "schemaVersion": 1,
  "tools": [
    {
      "category": "alignment",
      "command": "align.map",
      "description": "Aligns each query to a reference row of an MSA using tuned PairwiseAligner settings (match +2, mismatch -1, gap open -8, extend -0.5, cheap end gaps), records the residue→column map, and counts query residues inside feature column spans or reference-row coordinate ranges. Returns per query: length, n_aligned, identity, coverage, per-feature lengths + residues, and flags. Warns LOW_IDENTITY_MAPPING below min_identity (default 0.60) and FEATURE_SPAN_UNSTABLE because feature-span column counts are unreliable at low identity — always report identity with the lengths. reference_row='auto' tries every candidate row per query and keeps the highest-identity one (restrict with reference_rows); the chosen row is reported per query. Use feature_spans (0-based inclusive alignment columns) or feature_coords (1-based ungapped reference-row positions).",
      "examples": [
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
      ],
      "name": "lifesci_align_map",
      "parameters": {
        "additionalProperties": false,
        "oneOf": [
          {
            "required": [
              "queries"
            ]
          },
          {
            "required": [
              "query_fasta"
            ]
          }
        ],
        "properties": {
          "alignment": {
            "description": "Path to the reference alignment (FASTA, Stockholm, or Clustal; gzip ok). Rows may carry '/start-end' coordinate suffixes, which are matched leniently.",
            "type": "string"
          },
          "coords_reference_row": {
            "description": "Alignment row whose ungapped sequence defines feature_coords/reference coordinates (default: the reference row). Use the row the coordinates were annotated on when it differs from the mapping reference row.",
            "type": "string"
          },
          "feature_coords": {
            "additionalProperties": true,
            "description": "Feature definitions as 1-based ungapped reference-row coordinates, e.g. {'feature_a': [[11, 61]], 'feature_b': [[201, 261]]}; converted to column spans through the reference row.",
            "type": "object"
          },
          "feature_spans": {
            "additionalProperties": true,
            "description": "Feature definitions as alignment-column spans (0-based inclusive), e.g. {'feature_a': [[10, 60]], 'feature_b': [[200, 260]]}. A flat [start,end] pair or a list of column indices is also accepted.",
            "type": "object"
          },
          "features_file": {
            "description": "JSON file with feature definitions. Accepts a plain {feature: span(s)} map (treated as column spans by default), or keys feature_spans/feature_coords/coordinate_system.",
            "type": "string"
          },
          "min_identity": {
            "default": 0.6,
            "description": "Identity threshold below which LOW_IDENTITY_MAPPING and FEATURE_SPAN_UNSTABLE are raised.",
            "maximum": 1.0,
            "minimum": 0.0,
            "type": "number"
          },
          "out": {
            "description": "Optional path to write the mapping payload JSON.",
            "type": "string"
          },
          "queries": {
            "description": "Inline query sequences (alternative to query_fasta).",
            "items": {
              "additionalProperties": false,
              "properties": {
                "name": {
                  "description": "Query identifier used in the output.",
                  "type": "string"
                },
                "sequence": {
                  "description": "Ungapped (or gap-stripped) nucleotide/protein query sequence.",
                  "type": "string"
                }
              },
              "required": [
                "name",
                "sequence"
              ],
              "type": "object"
            },
            "minItems": 1,
            "type": "array"
          },
          "query_fasta": {
            "description": "Path to a FASTA file of query sequences (alternative to queries).",
            "type": "string"
          },
          "reference_row": {
            "description": "Name of the alignment row used as the reference coordinate system; the '/start-end' suffix may be omitted. Defaults to the first row. Use 'auto' to align each query to every candidate row and keep the row with the highest identity (ties keep the first row in file order) — the chosen row is reported per query in data.queries[].reference_row.",
            "type": "string"
          },
          "reference_rows": {
            "description": "Only with reference_row='auto': restrict the candidate reference rows to this list of names (recommended for large alignments; a warning is emitted when more than 50 candidates are tried).",
            "items": {
              "type": "string"
            },
            "type": "array"
          }
        },
        "required": [
          "alignment"
        ],
        "type": "object"
      },
      "timeoutMs": 300000,
      "title": "Map sequences onto a reference MSA row and measure feature spans"
    },
    {
      "category": "foundation",
      "command": "cache",
      "description": "Maintenance for the content-addressed HTTP cache that makes lifesci runs replayable offline. Actions: 'stats' (counts/bytes/databases/expired), 'list' (deterministic per-entry metadata), 'verify' (re-hash bodies; reports corruption), 'prune' (delete expired entries; honour dry_run), 'clear' (delete everything; requires confirm:true). This command never touches the network, so it is safe under --offline. Clearing the cache destroys the ability to reproduce prior results offline; verify first and export anything you need. Entries are sorted by cache key for deterministic output.",
      "examples": [
        {
          "action": "stats"
        },
        {
          "action": "list",
          "limit": 20
        },
        {
          "action": "prune",
          "dry_run": true
        },
        {
          "action": "clear",
          "confirm": true
        }
      ],
      "name": "lifesci_cache",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "action": {
            "description": "Cache operation: 'stats' summary counts; 'list' per-entry metadata; 'verify' re-hash cached bodies; 'prune' delete expired entries; 'clear' delete the whole cache (requires confirm:true).",
            "enum": [
              "stats",
              "list",
              "verify",
              "prune",
              "clear"
            ],
            "type": "string"
          },
          "confirm": {
            "default": false,
            "description": "For 'clear': must be true. This is a deliberate second confirmation because clearing destroys the offline replay cache.",
            "type": "boolean"
          },
          "database": {
            "description": "Only include entries recorded for this database name (e.g. 'uniprot', 'interpro'); omitted means every database.",
            "type": "string"
          },
          "dry_run": {
            "default": false,
            "description": "For 'prune': report what would be removed without deleting anything.",
            "type": "boolean"
          },
          "expired_only": {
            "default": false,
            "description": "For 'list'/'verify': only include entries whose TTL has expired.",
            "type": "boolean"
          },
          "limit": {
            "description": "Maximum number of entries to list or verify (deterministic key order). Omit for all entries.",
            "maximum": 100000,
            "minimum": 1,
            "type": "integer"
          }
        },
        "required": [
          "action"
        ],
        "type": "object"
      },
      "timeoutMs": 300000,
      "title": "Inspect and maintain the lifesci cache"
    },
    {
      "category": "structure",
      "command": "contacts",
      "description": "Measures minimum atom-atom distances between named structural features across one or more structures. RNA features may be selected by explicit residues ('101-120') or by alignment-column spans resolved through a residue→column colmap JSON; protein features use all atoms by default. Returns per pair: min_distance, closest atom/residue pair, n_under counts for thresholds (default 4/5/10 Å), and the percentile position of the contact along each element; missing features are reported explicitly, and bridges through a third feature trigger CONTACT_BRIDGE_THROUGH_THIRD_FEATURE. Always warns DISTANCE_ATOM_SELECTION (the value depends on the atom selection; P atoms ignore side chains) and CONTACT_ABSENT_NOT_PROOF (no contact in these structures/thresholds is not evidence of no interaction).",
      "examples": [
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
      ],
      "name": "lifesci_contacts",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "bridge_threshold": {
            "default": 4.5,
            "description": "Å threshold for bridge detection: warn when a third feature lies within this distance of both members of a non-contacting pair.",
            "maximum": 20,
            "minimum": 0,
            "type": "number"
          },
          "features": {
            "description": "Named features (rRNA elements or protein chains) to measure distances between.",
            "items": {
              "additionalProperties": false,
              "oneOf": [
                {
                  "required": [
                    "residues"
                  ]
                },
                {
                  "required": [
                    "columns"
                  ]
                }
              ],
              "properties": {
                "atoms": {
                  "description": "Atom-name priority list ('*' means all atoms). Default for rRNA chains: ['P'] with fallback ['C1\\'', 'C1*']; default for protein chains: ['*'] (all atoms, needed for realistic side-chain contacts).",
                  "items": {
                    "type": "string"
                  },
                  "type": "array"
                },
                "chain": {
                  "description": "Auth chain id containing the feature (required for residues mode; optional in columns mode where chains come from the colmap).",
                  "type": "string"
                },
                "colmap": {
                  "description": "Optional per-feature colmap override (path or inline object).",
                  "oneOf": [
                    {
                      "type": "string"
                    },
                    {
                      "additionalProperties": true,
                      "type": "object"
                    }
                  ]
                },
                "columns": {
                  "description": "Alignment-column selection resolved through the colmap: a colmap key such as 'feature_a_columns', an inclusive [start,end] span, a list of columns, or nested spans. Mutually exclusive with 'residues'.",
                  "oneOf": [
                    {
                      "type": "string"
                    },
                    {
                      "type": "array"
                    },
                    {
                      "type": "integer"
                    }
                  ]
                },
                "fallback_atoms": {
                  "description": "Fallback atom names used only when no primary atom exists (default ['C1\\'', 'C1*'] for rRNA).",
                  "items": {
                    "type": "string"
                  },
                  "type": "array"
                },
                "id": {
                  "description": "Feature id used in pairs (unique per structure; the same id may appear in several structures, e.g. feature_a in two structures).",
                  "type": "string"
                },
                "residues": {
                  "description": "Explicit residue selection: 'all', '101-120', '101,105,120', {'seqids': [...]}, or a resolved list of {'seqid','icode'} objects. Mutually exclusive with 'columns'.",
                  "oneOf": [
                    {
                      "type": "string"
                    },
                    {
                      "type": "array"
                    },
                    {
                      "additionalProperties": true,
                      "type": "object"
                    }
                  ]
                },
                "structure": {
                  "description": "Structure id this feature belongs to.",
                  "type": "string"
                }
              },
              "required": [
                "id",
                "structure"
              ],
              "type": "object"
            },
            "minItems": 1,
            "type": "array"
          },
          "out": {
            "description": "Optional path to write the contact payload JSON.",
            "type": "string"
          },
          "pairs": {
            "description": "Feature pairs to measure. Default: every pair of features within each structure.",
            "items": {
              "oneOf": [
                {
                  "items": {
                    "type": "string"
                  },
                  "maxItems": 2,
                  "minItems": 2,
                  "type": "array"
                },
                {
                  "additionalProperties": false,
                  "properties": {
                    "a": {
                      "description": "First feature id of the pair.",
                      "type": "string"
                    },
                    "b": {
                      "description": "Second feature id of the pair.",
                      "type": "string"
                    }
                  },
                  "required": [
                    "a",
                    "b"
                  ],
                  "type": "object"
                }
              ]
            },
            "type": "array"
          },
          "structures": {
            "description": "Structures to analyse; every feature belongs to exactly one of them.",
            "items": {
              "additionalProperties": false,
              "properties": {
                "colmap": {
                  "description": "Residue-index→alignment-column map for this structure: path to a colmap JSON or an inline object, e.g. {\"chains\": {\"AA\": {\"0\": 348}}, \"feature_a_columns\": [...], \"feature_b_columns\": [...]}.",
                  "oneOf": [
                    {
                      "type": "string"
                    },
                    {
                      "additionalProperties": true,
                      "type": "object"
                    }
                  ]
                },
                "id": {
                  "description": "Short structure identifier used in feature/pairs references (e.g. 'structure_a').",
                  "type": "string"
                },
                "path": {
                  "description": "Path to an mmCIF/PDB structure file (gzip allowed; a trimmed structure works).",
                  "type": "string"
                }
              },
              "required": [
                "id",
                "path"
              ],
              "type": "object"
            },
            "minItems": 1,
            "type": "array"
          },
          "thresholds": {
            "default": [
              4.0,
              5.0,
              10.0
            ],
            "description": "Distance thresholds (Å) for the n_under counts.",
            "items": {
              "minimum": 0,
              "type": "number"
            },
            "type": "array"
          },
          "top_pairs": {
            "default": 10,
            "description": "Number of closest atom pairs retained per feature pair (the minimum distance is always first).",
            "maximum": 50,
            "minimum": 1,
            "type": "integer"
          }
        },
        "required": [
          "structures",
          "features"
        ],
        "type": "object"
      },
      "timeoutMs": 600000,
      "title": "Inter-element contact distances"
    },
    {
      "category": "foundation",
      "command": "datasets",
      "description": "Local reference data (alignments, ES-length tables, curated matrices, structure collections) can be registered as read-only datasets with a recorded SHA-256. Actions: 'register' hashes a file/directory and records it (fails on a missing path or checksum mismatch); 'list' shows all datasets plus whether their files still exist; 'verify' re-hashes one dataset and reports every mismatching file; 'get' resolves a dataset (or one filename inside a directory dataset) and returns its verified path; 'remove' unregisters the record but never deletes data. The command never writes to dataset contents and refuses to register paths that do not exist.",
      "examples": [
        {
          "action": "list"
        },
        {
          "action": "register",
          "kind": "msa",
          "name": "reference_alignment",
          "path": "data/reference_alignment.fa"
        },
        {
          "action": "verify",
          "name": "reference_alignment"
        },
        {
          "action": "remove",
          "name": "reference_alignment"
        }
      ],
      "name": "lifesci_datasets",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "action": {
            "description": "Dataset registry action: 'register' a new read-only dataset with checksum; 'list' all registered datasets with existence/checksum status; 'verify' re-hash one dataset; 'get' resolve one dataset (optionally one file inside it); 'remove' unregister a dataset (never deletes data files).",
            "enum": [
              "register",
              "list",
              "verify",
              "get",
              "remove"
            ],
            "type": "string"
          },
          "description": {
            "description": "Free-text description of origin, version, and what the dataset contains.",
            "type": "string"
          },
          "filename": {
            "description": "For get: resolve this specific file inside a registered directory dataset.",
            "type": "string"
          },
          "kind": {
            "description": "Dataset kind; directories are hashed file-by-file into a manifest (register only).",
            "enum": [
              "fasta",
              "tsv",
              "json",
              "msa",
              "structure",
              "structure_dir",
              "processed_dir",
              "table",
              "hmm",
              "hmm_dir",
              "generic_dir"
            ],
            "type": "string"
          },
          "name": {
            "description": "Registered dataset name (required for register/verify/get/remove).",
            "pattern": "^[A-Za-z0-9_.-]{1,80}$",
            "type": "string"
          },
          "path": {
            "description": "Absolute or cwd-relative path to the dataset file or directory (register only).",
            "type": "string"
          },
          "verify": {
            "default": true,
            "description": "For register: hash and verify the payload before recording it (recommended).",
            "type": "boolean"
          }
        },
        "type": "object"
      },
      "timeoutMs": 600000,
      "title": "Manage local read-only datasets"
    },
    {
      "category": "foundation",
      "command": "doctor",
      "description": "Environment preflight for the lifesci tool layer. Reports Python/package versions, Foldseek/PyMOL/SSH availability, cache writability and contents, registered read-only datasets, pinned database releases, and (unless offline) reachability of every public portal. Run this first when a lifesci call fails strangely. It never fails the command because a portal is down: read data.status ('ok'|'degraded'|'offline') and data.checks.",
      "examples": [
        {
          "network": false
        }
      ],
      "name": "lifesci_doctor",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "foldseek_path": {
            "description": "Explicit Foldseek binary path.",
            "type": "string"
          },
          "network": {
            "default": true,
            "description": "Probe public portals (automatically skipped in offline mode).",
            "type": "boolean"
          },
          "portals": {
            "description": "Optional subset of portal probe names to check.",
            "items": {
              "type": "string"
            },
            "type": "array"
          },
          "probe_timeout_s": {
            "default": 10,
            "description": "Per-portal reachability timeout.",
            "maximum": 30,
            "minimum": 1,
            "type": "number"
          },
          "pymol_path": {
            "description": "Explicit PyMOL executable path.",
            "type": "string"
          }
        },
        "type": "object"
      },
      "timeoutMs": 120000,
      "title": "Check lifesci environment"
    },
    {
      "category": "sequence",
      "command": "domains",
      "description": "Annotate proteins with domains/families and deliver an explicit per-domain verdict. Three evidence sources can be combined: (1) a cached InterPro entry-API scan for UniProt accessions; (2) recorded per-protein domain-evidence rows ('evidence_rows'/'rows_file', e.g. a recorded Pfam proteome scan) for fully offline replay; (3) a local pyhmmer hmmsearch against supplied sequences/HMMs. Each requested domain gets present/weak/absent/unknown with its E-value, the database/release and the cutoff used, so an absent call is an explicit annotation statement with its reporting threshold (e.g. 'no PF00042 hit at report E<=1000'), never proof of functional absence. The domain arbiter can compare optional sequence-level (phmmer) evidence: a significant domain with no sequence hit is reported as domain-arbiter-present because domain/family evidence outranks a single weak sequence hit. Caveats: local hmmsearch E-values scale with the number of target sequences scanned and are not directly comparable to proteome-scan or InterPro-reported values (a warning is emitted); InterPro's per-location 'score' field has portal-specific semantics and for Pfam matches is an E-value-like score.",
      "examples": [
        {
          "accessions": [
            "P69905",
            "P00698"
          ],
          "domains": [
            "PF00042"
          ]
        },
        {
          "domains": [
            "PF00042"
          ],
          "rows_file": "evidence/pfam_hits.tsv"
        },
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
      ],
      "name": "lifesci_domains",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "accessions": {
            "description": "UniProt accessions for a remote InterPro scan (cached; replays offline when the InterPro payloads are already in the cache).",
            "items": {
              "minLength": 1,
              "type": "string"
            },
            "type": "array"
          },
          "cpus": {
            "default": 4,
            "description": "CPU threads for the local pyhmmer scan.",
            "maximum": 64,
            "minimum": 1,
            "type": "integer"
          },
          "domains": {
            "description": "Domain/family accessions to arbitrate explicitly (e.g. ['PF00042']). Each gets a present/weak/absent/unknown verdict even when no annotation is returned. When omitted, every model found in the merged evidence is reported.",
            "items": {
              "minLength": 1,
              "type": "string"
            },
            "type": "array"
          },
          "evidence_rows": {
            "description": "Recorded per-protein domain-evidence rows (offline replay). Accepts the standard presence formats: keys accession/protein/best_hit, model/pfam_model, evalue, score, model_cov, protein_length, description, source, release, target_db_size.",
            "items": {
              "type": "object"
            },
            "type": "array"
          },
          "hmm_ids": {
            "description": "Pfam model accessions to download from InterPro (wwwapi?annotation=hmm, cached) and scan locally, e.g. ['PF00042'].",
            "items": {
              "type": "string"
            },
            "type": "array"
          },
          "hmms": {
            "description": "Local HMMER profile files to scan with (no remote model fetch).",
            "items": {
              "additionalProperties": false,
              "properties": {
                "id": {
                  "description": "Model accession/name, e.g. PF00042.",
                  "type": "string"
                },
                "path": {
                  "description": "Local .hmm/.hmm.gz file path.",
                  "type": "string"
                }
              },
              "required": [
                "path"
              ],
              "type": "object"
            },
            "type": "array"
          },
          "max_hits": {
            "default": 200,
            "description": "Maximum recorded local HMM hits.",
            "maximum": 5000,
            "minimum": 1,
            "type": "integer"
          },
          "out": {
            "description": "Optional directory for merged domain records written as JSON.",
            "type": "string"
          },
          "present_evalue": {
            "default": 0.001,
            "description": "E-value cutoff for a 'present' domain call (default 1e-3).",
            "minimum": 0,
            "type": "number"
          },
          "report_evalue": {
            "default": 10.0,
            "description": "HMMER reporting threshold for the local scan. Use a permissive value (e.g. 1000) to state explicitly 'no hit at E<=N' for an absent domain; the cutoff is reported with the absent verdict.",
            "minimum": 0,
            "type": "number"
          },
          "rows_file": {
            "description": "TSV/JSON file of recorded domain-evidence rows (or a curated table). Relative paths resolve against the working directory.",
            "type": "string"
          },
          "sequence_evidence": {
            "additionalProperties": {
              "type": "object"
            },
            "description": "Optional sequence-level (phmmer) evidence to compare with the domain calls, keyed by accession: {'detectable': bool|'weak'|'absent', 'best_evalue': number|null, 'source': str}. A significant domain with no sequence hit is reported as domain-arbiter-present.",
            "type": "object"
          },
          "sequences": {
            "additionalProperties": {
              "type": "string"
            },
            "description": "Inline target sequences for the local HMM scan, keyed by accession.",
            "type": "object"
          },
          "targets_fasta": {
            "description": "FASTA file of target sequences for the local HMM scan.",
            "type": "string"
          },
          "weak_evalue": {
            "default": 1.0,
            "description": "E-value cutoff below which a domain call is 'weak' rather than absent.",
            "minimum": 0,
            "type": "number"
          }
        },
        "type": "object"
      },
      "timeoutMs": 600000,
      "title": "Domain/family annotation and domain arbiter"
    },
    {
      "category": "visualisation",
      "command": "figures",
      "description": "Renders publication-style figures with the headless matplotlib Agg backend: a log-log covariance scatter with regression line and annotated outliers, a distribution histogram, or a contact-distance bar chart. Data may come from a table (JSON/TSV/CSV), inline rows, or a saved lifesci.stats payload; the source SHA-256 and regression parameters are recorded (FIGURE_DATA_PROVENANCE) so figures are reproducible. Optionally builds a PyMOL session from an opaque spec via lib.pymol_session: emits a .pml always, validates/renders with `pymol -cq` only when PyMOL is installed, and reports honest PASS/FAIL/SKIPPED. Warns PYMOL_MISSING when PyMOL is absent and PNG_RENDER_LIMITATION when headless full-session rendering fails (render small extracted structures instead).",
      "examples": [
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
      ],
      "name": "lifesci_figures",
      "parameters": {
        "additionalProperties": false,
        "anyOf": [
          {
            "required": [
              "figures"
            ]
          },
          {
            "required": [
              "pymol_spec"
            ]
          }
        ],
        "properties": {
          "figures": {
            "description": "Figures to render.",
            "items": {
              "additionalProperties": false,
              "properties": {
                "annotate_outliers": {
                  "default": true,
                  "description": "covariance_loglog: annotate standardised-residual outliers.",
                  "type": "boolean"
                },
                "bins": {
                  "default": 30,
                  "description": "length_histogram: number of bins.",
                  "maximum": 200,
                  "minimum": 2,
                  "type": "integer"
                },
                "column": {
                  "description": "length_histogram: numeric column to plot.",
                  "type": "string"
                },
                "data": {
                  "additionalProperties": false,
                  "description": "Figure data source: one of table/rows/stats plus an optional row filter.",
                  "properties": {
                    "filter": {
                      "additionalProperties": true,
                      "description": "Optional name_prefix/name_regex/ids row filter (same shape as lifesci.stats).",
                      "type": "object"
                    },
                    "rows": {
                      "description": "Inline records (alternative to table).",
                      "items": {
                        "additionalProperties": true,
                        "type": "object"
                      },
                      "type": "array"
                    },
                    "stats": {
                      "description": "Path to a saved lifesci.stats payload/envelope; its 'points' are used as the figure rows.",
                      "type": "string"
                    },
                    "table": {
                      "description": "Path to a JSON/TSV/CSV table (relative paths resolve against ctx.cwd).",
                      "type": "string"
                    }
                  },
                  "type": "object"
                },
                "filename": {
                  "description": "Output PNG filename (default derived from kind + index).",
                  "type": "string"
                },
                "group_by": {
                  "description": "covariance_loglog: column used for colouring. 'auto' groups by the identifier prefix before the first '_' or '-'; 'none' disables grouping.",
                  "type": "string"
                },
                "kind": {
                  "description": "'covariance_loglog': log-log scatter with regression + annotated outliers; 'length_histogram': distribution of one numeric column; 'contact_bars': bar chart of one value column by label (e.g. contact minimum distances).",
                  "enum": [
                    "covariance_loglog",
                    "length_histogram",
                    "contact_bars"
                  ],
                  "type": "string"
                },
                "label_column": {
                  "description": "contact_bars: category label column (e.g. 'pair'); default: row id.",
                  "type": "string"
                },
                "regression": {
                  "default": true,
                  "description": "covariance_loglog: draw the log-log regression line.",
                  "type": "boolean"
                },
                "title": {
                  "description": "Figure title.",
                  "type": "string"
                },
                "value_column": {
                  "description": "contact_bars: numeric value column (e.g. 'min_distance').",
                  "type": "string"
                },
                "x": {
                  "description": "covariance_loglog: x column (required).",
                  "type": "string"
                },
                "xlabel": {
                  "description": "X-axis label.",
                  "type": "string"
                },
                "y": {
                  "description": "covariance_loglog: y column (required).",
                  "type": "string"
                },
                "ylabel": {
                  "description": "Y-axis label.",
                  "type": "string"
                }
              },
              "required": [
                "kind",
                "data"
              ],
              "type": "object"
            },
            "minItems": 1,
            "type": "array"
          },
          "out_dir": {
            "description": "Directory for generated figures (default: <ctx.cwd>/lifesci-figures).",
            "type": "string"
          },
          "pymol_out_dir": {
            "description": "Directory for the generated .pml/.pse session (default: <out_dir>/pymol).",
            "type": "string"
          },
          "pymol_spec": {
            "additionalProperties": true,
            "description": "Opaque PyMOL session spec passed verbatim to lifesci.lib.pymol_session: {name, objects:[{path, name?, load_type?, color?, selections?}], commands?, scenes?, save_pse?, render_png?:{path,width,height,dpi}}.",
            "type": "object"
          }
        },
        "type": "object"
      },
      "timeoutMs": 600000,
      "title": "Generate quantitative figures and PyMOL sessions"
    },
    {
      "category": "foundation",
      "command": "http",
      "description": "Generic HTTP primitive for endpoints without a dedicated lifesci adapter. Goes through the same content-addressed cache, per-host rate limiting, exponential backoff, offline replay and provenance logging as every adapter. Returns request metadata, response status/headers/SHA-256/bytes/cache-hit/attempts and, when 'parse' allows it, the decoded body. In --dry-run it returns the planned request without touching the network; in --offline it replays a cached payload only. Caveats: E-values/TM-scores in arbitrary returned payloads carry the semantics of the source database (not comparable across databases, often query-normalised and top-hit capped) - this command only guarantees provenance, not scientific interpretation.",
      "examples": [
        {
          "parse": "text",
          "url": "https://rest.uniprot.org/uniprotkb/P69905.fasta"
        },
        {
          "parse": "json",
          "url": "https://www.ebi.ac.uk/interpro/api/entry/pfam/PF00042/"
        }
      ],
      "name": "lifesci_http",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "allow_status": {
            "default": [
              200
            ],
            "description": "HTTP statuses treated as success. Non-listed statuses raise a PortalError with the status appended to the error code (e.g. HTTP_ERROR_404).",
            "items": {
              "maximum": 599,
              "minimum": 100,
              "type": "integer"
            },
            "type": "array"
          },
          "body": {
            "description": "Raw request body as text (UTF-8 encoded). Mutually exclusive with other body fields.",
            "type": "string"
          },
          "body_base64": {
            "description": "Raw request body as base64 (for binary payloads). Mutually exclusive with other body fields.",
            "type": "string"
          },
          "cache": {
            "default": true,
            "description": "Read/write the content-addressed cache. Set false for polling endpoints whose URL changes or that must be re-requested.",
            "type": "boolean"
          },
          "cache_ttl": {
            "description": "Override the cache TTL in seconds for this response.",
            "minimum": 0,
            "type": "number"
          },
          "database": {
            "description": "Optional database label recorded in the provenance cache entry (e.g. 'uniprot').",
            "type": "string"
          },
          "files": {
            "description": "Multipart file uploads. Only multipart file upload works for some APIs (e.g. Foldseek web ticket); JSON bodies are rejected by those servers.",
            "items": {
              "additionalProperties": false,
              "properties": {
                "content_type": {
                  "default": "application/octet-stream",
                  "description": "MIME type sent for this file part.",
                  "type": "string"
                },
                "field": {
                  "description": "Form field name, e.g. 'q'.",
                  "type": "string"
                },
                "filename": {
                  "description": "Filename sent to the server (defaults to the basename of 'path').",
                  "type": "string"
                },
                "path": {
                  "description": "Local file to upload; relative paths resolve against the working directory.",
                  "type": "string"
                }
              },
              "required": [
                "field",
                "path"
              ],
              "type": "object"
            },
            "type": "array"
          },
          "form_fields": {
            "additionalProperties": {
              "type": [
                "string",
                "number",
                "boolean"
              ]
            },
            "description": "urlencoded form fields. When 'files' is also given, these become the ordinary multipart/form-data fields of the upload.",
            "type": "object"
          },
          "headers": {
            "additionalProperties": {
              "type": "string"
            },
            "description": "Extra request headers. The lifesci User-Agent is added automatically. Authorization/token headers are never cached or written to the provenance log.",
            "type": "object"
          },
          "idempotent": {
            "description": "Force or forbid retries for a write request. Defaults to true for GET/HEAD/OPTIONS and false for body-bearing methods.",
            "type": "boolean"
          },
          "json_body": {
            "description": "Request body serialised as canonical JSON (sets Content-Type: application/json).",
            "type": [
              "object",
              "array"
            ]
          },
          "max_parse_bytes": {
            "default": 2000000,
            "description": "Do not echo parsed bodies larger than this many bytes into the envelope; write them with 'out' instead. Prevents multi-megabyte tool results.",
            "minimum": 0,
            "type": "integer"
          },
          "method": {
            "default": "GET",
            "description": "HTTP method. GET/HEAD/OPTIONS are retried on transient failures; POST/PUT/PATCH/DELETE are only retried when 'idempotent' is true.",
            "enum": [
              "GET",
              "HEAD",
              "POST",
              "PUT",
              "PATCH",
              "DELETE"
            ],
            "type": "string"
          },
          "out": {
            "description": "Optional path: write the raw response body here and record its SHA-256. Relative paths resolve against the working directory.",
            "type": "string"
          },
          "params": {
            "additionalProperties": {
              "type": [
                "string",
                "number",
                "boolean",
                "null"
              ]
            },
            "description": "Query parameters; values are converted to strings and sorted before use.",
            "type": "object"
          },
          "parse": {
            "default": "auto",
            "description": "Decode the response body into the payload. 'auto' parses JSON when the content type looks like JSON, otherwise returns a truncated text preview. 'none' returns metadata only (useful with 'out').",
            "enum": [
              "auto",
              "none",
              "json",
              "text"
            ],
            "type": "string"
          },
          "release_header": {
            "description": "Response header carrying the database release (e.g. 'x-uniprot-release'); its value is recorded in the release registry and provenance.",
            "type": "string"
          },
          "retries": {
            "description": "Transient retry budget for this request (overrides the context default).",
            "maximum": 20,
            "minimum": 0,
            "type": "integer"
          },
          "text_preview_bytes": {
            "default": 4000,
            "description": "Maximum characters of a text body echoed into the payload.",
            "minimum": 0,
            "type": "integer"
          },
          "timeout": {
            "description": "Per-attempt timeout in seconds (overrides the context default).",
            "minimum": 0.1,
            "type": "number"
          },
          "url": {
            "description": "Absolute http(s) URL without query string. Query parameters go in 'params' so they are sorted deterministically, recorded in provenance, and form part of the cache key.",
            "minLength": 1,
            "type": "string"
          }
        },
        "required": [
          "url"
        ],
        "type": "object"
      },
      "timeoutMs": 180000,
      "title": "Raw cached HTTP request"
    },
    {
      "category": "sequence",
      "command": "orthology",
      "description": "Look up family/orthogroup membership for one or more proteins across four independent sources and arbitrate them explicitly: PANTHER (via the InterPro entry API and UniProt cross-references), OrthoDB (orthogroup API), eggNOG (UniProt cross-reference; no mapper call) and OMA (OMA browser API). Returns per-source records with identifiers, labels, availability and reason, a pairwise cross-check table with agree/disagree/unknown, and an overall agreement per protein. Missing/unreachable sources are warnings, not errors; a single agreeing source is reported as unknown confidence, and any disagreement is flagged for human review. Caveats: identifier namespaces are not interchangeable across databases, agreement here means the sources carry consistent family vocabulary, not that orthology is proven; divergent fast-evolving families can be annotated by only one source.",
      "examples": [
        {
          "accessions": [
            "P69905"
          ]
        },
        {
          "accessions": [
            "P69905",
            "P00698"
          ],
          "expected_family": "globin"
        }
      ],
      "name": "lifesci_orthology",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "accessions": {
            "description": "UniProt accessions to annotate. Each protein is queried across the selected orthology sources; missing sources are warnings, not errors.",
            "items": {
              "minLength": 1,
              "type": "string"
            },
            "minItems": 1,
            "type": "array"
          },
          "expected_family": {
            "description": "Optional family label to test every source against. When omitted, the expected family is derived from the UniProt protein/gene name.",
            "type": "string"
          },
          "max_pages": {
            "default": 2,
            "description": "Maximum InterPro result pages fetched per protein.",
            "maximum": 20,
            "minimum": 1,
            "type": "integer"
          },
          "out": {
            "description": "Optional directory to write the orthology record JSON (per accession).",
            "type": "string"
          },
          "page_size": {
            "default": 50,
            "description": "InterPro entry-API page size used for the PANTHER lookup.",
            "maximum": 200,
            "minimum": 1,
            "type": "integer"
          },
          "sources": {
            "default": [
              "panther",
              "orthodb",
              "eggnog",
              "oma"
            ],
            "description": "Which orthology sources to query. PANTHER/eggNOG come from UniProt cross-references and the InterPro entry API; OrthoDB and OMA come from their own APIs.",
            "items": {
              "enum": [
                "panther",
                "orthodb",
                "eggnog",
                "oma"
              ],
              "type": "string"
            },
            "type": "array"
          }
        },
        "required": [
          "accessions"
        ],
        "type": "object"
      },
      "timeoutMs": 300000,
      "title": "Cross-check protein orthology sources"
    },
    {
      "category": "foundation",
      "command": "remote",
      "description": "Offload heavy compute to configured lab machines over SSH. Remote execution is OFF unless LIFESCI_REMOTE_HOSTS / LIFESCI_REMOTE_CONFIG are configured, and is never required for offline work. Actions: 'list' returns host aliases and connection metadata with no secrets (key paths never appear); 'plan' returns the ssh plan without executing; 'run' executes the job and records provenance (host, argv, start/end, exit code, remote tool version). run requires allow:true AND host+argv; use absolute tool paths because remote PATHs are minimal. stdout/stderr in the payload are truncated (use out to save the full job result). The plugin never writes credentials and never auto-executes downloaded code.",
      "examples": [
        {
          "action": "list"
        },
        {
          "action": "plan",
          "argv": [
            "/opt/hhsuite/bin/hhsearch",
            "-h"
          ],
          "host": "gpu01"
        }
      ],
      "name": "lifesci_remote",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "action": {
            "description": "'list' configured host aliases (never leaks credentials); 'plan' build the ssh job plan without executing it; 'run' execute the job over ssh (requires allow:true, and remote execution must be configured).",
            "enum": [
              "list",
              "plan",
              "run"
            ],
            "type": "string"
          },
          "allow": {
            "default": false,
            "description": "Explicit opt-in required for action='run'. Without it, run refuses; use action='plan' or --dry-run to inspect the job first.",
            "type": "boolean"
          },
          "argv": {
            "description": "Remote command argv (no shell string), e.g. ['/opt/hhsuite/bin/hhsearch', '-i', 'query.a3m', '-d', '/data/hhsuite/db']. Prefer absolute tool paths because remote PATHs are minimal.",
            "items": {
              "type": "string"
            },
            "minItems": 1,
            "type": "array"
          },
          "cwd": {
            "description": "Remote working directory for the job (optional; host work_dir otherwise).",
            "type": "string"
          },
          "host": {
            "description": "Configured host alias (e.g. 'gpu01'), required for plan/run.",
            "type": "string"
          },
          "out": {
            "description": "Optional path to write the full job result JSON (stdout/stderr are truncated in data).",
            "type": "string"
          },
          "timeout": {
            "default": 600,
            "description": "Maximum wall-clock seconds for the remote job (default 600).",
            "maximum": 86400,
            "minimum": 1,
            "type": "number"
          }
        },
        "required": [
          "action"
        ],
        "type": "object"
      },
      "timeoutMs": 90000,
      "title": "Opt-in remote execution over SSH"
    },
    {
      "category": "reporting",
      "command": "report",
      "description": "Builds a reproducible Markdown report from structured, evidence-graded claims. Every claim MUST carry a grade (ESTABLISHED, INFERRED, UNKNOWN) and ESTABLISHED/INFERRED claims MUST carry at least one PMID/DOI/URL citation; the command refuses otherwise (EVIDENCE_GRADE_MISSING / EVIDENCE_CITATION_MISSING) and never silently downgrades a grade. Output includes the per-section claims, an explicit caveats list, and a provenance table assembled from the command envelope provenance (HTTP fetches with URL/SHA-256/release, local file hashes, tool runs) plus any records supplied in 'provenance'. Deterministic for identical input. Quantitative claims should state value, n, test, measure definition, database/version, and date.",
      "examples": [
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
      ],
      "name": "lifesci_report",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "caveats": {
            "description": "Explicit caveats/limitations (e.g. 'no detectable homolog is not proof of absence', 'correlation is not causation'). Recommended; a warning is emitted when empty.",
            "items": {
              "type": "string"
            },
            "type": "array"
          },
          "generated_at": {
            "description": "Deterministic generation timestamp rendered in the report. Default: the latest timestamp among the included provenance records, else 'not-recorded'. Pass an explicit ISO-8601 value for reproducible report bytes.",
            "type": "string"
          },
          "include_envelope_provenance": {
            "default": true,
            "description": "Include the current command envelope's provenance records in the report provenance table.",
            "type": "boolean"
          },
          "out": {
            "description": "Markdown output path (default: <ctx.cwd>/lifesci-report.md).",
            "type": "string"
          },
          "provenance": {
            "description": "Additional provenance records (kind/time/url/sha256/release...) to include in the table.",
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "type": "array"
          },
          "sections": {
            "description": "Report sections, each with graded claims.",
            "items": {
              "additionalProperties": false,
              "properties": {
                "claims": {
                  "description": "Claims in this section (may be empty for an intro-only section).",
                  "items": {
                    "additionalProperties": false,
                    "properties": {
                      "citations": {
                        "description": "Citations (PMID/DOI/URL) for the claim. Required for every claim, including UNKNOWN claims (cite the observation that leaves the question unresolved).",
                        "items": {
                          "additionalProperties": false,
                          "properties": {
                            "doi": {
                              "description": "DOI.",
                              "type": "string"
                            },
                            "pmid": {
                              "description": "PubMed ID.",
                              "type": "string"
                            },
                            "title": {
                              "description": "Optional citation title.",
                              "type": "string"
                            },
                            "url": {
                              "description": "Any other stable URL (e.g. PDB entry).",
                              "type": "string"
                            }
                          },
                          "type": "object"
                        },
                        "type": "array"
                      },
                      "grade": {
                        "description": "Evidence grade. ESTABLISHED: directly shown by data/literature; INFERRED: supported by indirect or single-line evidence; UNKNOWN: not determined (explicitly unresolved). Missing/invalid grades are refused with EVIDENCE_GRADE_MISSING.",
                        "enum": [
                          "ESTABLISHED",
                          "INFERRED",
                          "UNKNOWN"
                        ],
                        "type": "string"
                      },
                      "id": {
                        "description": "Optional short claim id (e.g. 'C1') used in cross-references.",
                        "type": "string"
                      },
                      "note": {
                        "description": "Optional reviewer note (e.g. why a claim is INFERRED).",
                        "type": "string"
                      },
                      "text": {
                        "description": "The claim. Quantitative claims must state value, n, test, measure definition, database/version, and date.",
                        "type": "string"
                      }
                    },
                    "required": [
                      "text"
                    ],
                    "type": "object"
                  },
                  "type": "array"
                },
                "heading": {
                  "description": "Section heading.",
                  "type": "string"
                },
                "intro": {
                  "description": "Optional introductory paragraph.",
                  "type": "string"
                }
              },
              "required": [
                "heading"
              ],
              "type": "object"
            },
            "minItems": 1,
            "type": "array"
          },
          "title": {
            "description": "Report title.",
            "type": "string"
          }
        },
        "required": [
          "title",
          "sections"
        ],
        "type": "object"
      },
      "timeoutMs": 120000,
      "title": "Assemble an evidence-graded Markdown report"
    },
    {
      "category": "sequence",
      "command": "seq.fetch",
      "description": "Fetch UniProt data reproducibly: per-accession FASTA sequences (returned inline and written to 'out'), optional normalised entry metadata (protein name, organism/taxid, cross-references to PANTHER/eggNOG/OrthoDB/OMA/AlphaFold, features, comments, references), and reference-proteome FASTAs. The UniProt release is recorded for every payload; REST proteome streaming falls back to the release-pinned FTP/EBI reference-proteome FASTA when rate-limited or empty (a warning tells you which path was used; non-reference proteomes may not exist on FTP). Failures for individual accessions are returned in errors[] while the remaining fetches continue. Caveats: protein E-values and BLAST/HMMER counts computed against different UniProt releases or proteome sets are not strictly comparable - always carry the recorded release forward.",
      "examples": [
        {
          "accessions": [
            "P69905"
          ],
          "fetch_entries": true
        },
        {
          "proteomes": [
            {
              "proteome_id": "UP000005640",
              "taxid": 9606
            }
          ]
        }
      ],
      "name": "lifesci_seq_fetch",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "accessions": {
            "description": "UniProt accessions to fetch. Each yields a FASTA sequence record; the same accession is also looked up as an entry when 'fetch_entries' is true. Unknown accessions are reported in errors[] without failing the other fetches.",
            "items": {
              "minLength": 1,
              "type": "string"
            },
            "type": "array"
          },
          "fetch_entries": {
            "default": false,
            "description": "Also fetch and normalise the full UniProtKB entry (protein name, organism, length, cross-references, features, comments). Needed by downstream orthology and provenance reporting; costs one request per accession.",
            "type": "boolean"
          },
          "out": {
            "description": "Output directory for sequence FASTA files and downloaded proteomes (default: <cache-dir>/out/seq.fetch). Relative paths resolve against the working directory.",
            "type": "string"
          },
          "proteomes": {
            "description": "Reference proteomes to download. Provide either 'proteome_id' (UP...) or 'taxid' (resolved through the UniProt proteomes API). The REST stream is tried first; on HTTP 429/5xx/empty it falls back to the release-pinned EBI/FTP reference-proteome FASTA and warns (some non-reference proteomes are only available via REST).",
            "items": {
              "additionalProperties": false,
              "properties": {
                "out_dir": {
                  "description": "Directory for this proteome FASTA (defaults to the command 'out' directory).",
                  "type": "string"
                },
                "proteome_id": {
                  "description": "UniProt proteome identifier, e.g. UP000005640.",
                  "type": "string"
                },
                "taxid": {
                  "description": "NCBI species taxid; resolved to a proteome id when 'proteome_id' is absent.",
                  "minimum": 1,
                  "type": "integer"
                }
              },
              "type": "object"
            },
            "type": "array"
          },
          "strict": {
            "default": false,
            "description": "Raise on the first accession/proteome failure instead of collecting failures in errors[] and continuing. The command always raises when every requested item fails.",
            "type": "boolean"
          },
          "write_sequences": {
            "default": true,
            "description": "Write one <accession>.fasta file per fetched sequence into 'out'. Disable for a pure metadata query; the sequence is still returned inline in sequences[].",
            "type": "boolean"
          }
        },
        "type": "object"
      },
      "timeoutMs": 600000,
      "title": "Fetch UniProt sequences, entries and proteomes"
    },
    {
      "category": "sequence",
      "command": "seq.search",
      "description": "Detect homologs and build a presence/absence matrix with per-cell evidence. Modes: 'phmmer' runs local pyhmmer phmmer (compositional-bias filter OFF by design; reciprocal best-hit checks) and can add a Pfam/HMM domain scan that curates cells; 'hmmsearch' scans profile HMMs against proteomes and curates them with caller-supplied factor signatures (unmapped models become factors named by model id); 'evidence' replays recorded per-model Pfam hit rows (pfam_hits.tsv format), optional sequence rows and reciprocal-check rows offline and curates a presence matrix with evidence strings. Factors and their models are declared via factor_queries / factor_models. Mandatory caveats are always reported: absent means 'no detectable homolog' not proof of absence; E-values depend on database size and are not comparable across proteomes/databases; single-sequence search misses very divergent proteins, so domain/family evidence is the arbiter; weak calls are flagged for human review. Reciprocal checks are reported when available; without them sequence-level exclusions are not corroborated.",
      "examples": [
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
        },
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
        },
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
      ],
      "name": "lifesci_seq_search",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "E": {
            "default": 10.0,
            "description": "HMMER reporting E-value threshold for sequence/domain searches.",
            "minimum": 0,
            "type": "number"
          },
          "bias_filter": {
            "default": false,
            "description": "Compositional-bias filter. Keep false (default): the filter can hide genuine divergent homologues. Setting true emits a stronger warning.",
            "type": "boolean"
          },
          "cells": {
            "description": "evidence mode: explicit cells [{organism, factor, hits: [{protein, model, evalue, ...}], sequence_hit?}]. Used instead of grouping flat rows.",
            "items": {
              "type": "object"
            },
            "type": "array"
          },
          "cpus": {
            "default": 4,
            "description": "CPU threads for pyhmmer searches.",
            "maximum": 64,
            "minimum": 1,
            "type": "integer"
          },
          "expected": {
            "description": "evidence mode: expected result to verify reproduction, e.g. {'matrix': {org: {factor: verdict}}, 'evidence': {org: {factor: 'string'}}} or a cells list. Mismatches are reported and warned.",
            "type": "object"
          },
          "expected_file": {
            "description": "evidence mode: JSON file with the expected matrix/evidence.",
            "type": "string"
          },
          "factor_models": {
            "additionalProperties": {
              "items": {
                "type": "string"
              },
              "type": "array"
            },
            "description": "Optional mapping of factor -> Pfam model accessions used for curation, e.g. {'globin': ['PF00042']}. There are no built-in factors: supply factor_models (or factor_queries) to curate a presence matrix.",
            "type": "object"
          },
          "factor_queries": {
            "additionalProperties": {
              "items": {
                "type": "string"
              },
              "type": "array"
            },
            "description": "Presence-matrix design: {factor: [query name, ...]} mapping the phmmer query names to factors (e.g. {'globin': ['globin:P69905']}). Required to build cells/matrix in phmmer mode without a domain scan.",
            "type": "object"
          },
          "hmm_ids": {
            "description": "Pfam model accessions (e.g. PF00042) downloaded from InterPro (cached) for the local domain scan.",
            "items": {
              "type": "string"
            },
            "type": "array"
          },
          "hmms": {
            "description": "Local HMM models for the domain scan (also used by hmmsearch mode).",
            "items": {
              "additionalProperties": false,
              "properties": {
                "id": {
                  "description": "Model accession/name, e.g. PF00042.",
                  "type": "string"
                },
                "path": {
                  "description": "Local HMMER model file path.",
                  "type": "string"
                }
              },
              "required": [
                "path"
              ],
              "type": "object"
            },
            "type": "array"
          },
          "max_hits": {
            "default": 100,
            "description": "Maximum hits kept per search.",
            "maximum": 10000,
            "minimum": 1,
            "type": "integer"
          },
          "mode": {
            "description": "Search backend. 'phmmer' runs pyhmmer phmmer (bias_filter=False) and optional reciprocal/domain checks; 'hmmsearch' scans profile HMMs against proteomes; 'evidence' replays recorded per-model hit rows offline and curates a presence matrix.",
            "enum": [
              "phmmer",
              "hmmsearch",
              "evidence"
            ],
            "type": "string"
          },
          "out": {
            "description": "Optional directory for written hits TSV / matrix JSON.",
            "type": "string"
          },
          "present_evalue": {
            "default": 0.001,
            "description": "E-value cutoff for a 'present' presence call (sequence-only matrices; default 1e-3).",
            "minimum": 0,
            "type": "number"
          },
          "queries": {
            "additionalProperties": {
              "type": "string"
            },
            "description": "phmmer queries as {name: protein_sequence}. Prefer descriptive names such as 'globin:P69905'; pair them with 'factor_queries' for a presence matrix.",
            "type": "object"
          },
          "queries_file": {
            "description": "FASTA file of phmmer queries (header first token = query name).",
            "type": "string"
          },
          "query_accessions": {
            "description": "UniProt accessions fetched and used as phmmer queries (one request each).",
            "items": {
              "type": "string"
            },
            "type": "array"
          },
          "reciprocal": {
            "default": true,
            "description": "phmmer mode only: run the reverse search of forward best hits against the query database and report reciprocal best hits. Required for excluding homologues.",
            "type": "boolean"
          },
          "reciprocal_file": {
            "description": "evidence mode: TSV file of reciprocal-check rows (reciprocal_check.tsv format).",
            "type": "string"
          },
          "reciprocal_rows": {
            "description": "evidence mode: inline reciprocal-check rows (reciprocal_check.tsv columns).",
            "items": {
              "type": "object"
            },
            "type": "array"
          },
          "rows": {
            "description": "evidence mode: inline recorded per-model hit rows (pfam_hits.tsv columns: organism, taxid, proteome, pfam_model, model_name, best_hit, evalue, score, model_cov, protein_length, description). Multiple rows per organism/model are expected.",
            "items": {
              "type": "object"
            },
            "type": "array"
          },
          "rows_file": {
            "description": "evidence mode: TSV/JSON file of recorded per-model hit rows, or a list of such files.",
            "oneOf": [
              {
                "type": "string"
              },
              {
                "items": {
                  "type": "string"
                },
                "type": "array"
              }
            ]
          },
          "sequence_rows": {
            "description": "evidence mode: inline phmmer sequence-hit rows (presence_phmmer.tsv columns: organism, factor, best_hit, evalue, score, description, verdict) attached to cells as sequence_hit.",
            "items": {
              "type": "object"
            },
            "type": "array"
          },
          "sequence_rows_file": {
            "description": "evidence mode: TSV file of phmmer sequence-hit rows.",
            "type": "string"
          },
          "targets": {
            "description": "Target proteomes/FASTAs for phmmer/hmmsearch. For presence curation each target should carry an 'organism' label.",
            "items": {
              "additionalProperties": false,
              "properties": {
                "fasta": {
                  "description": "Target proteome FASTA path (local file; .gz accepted).",
                  "type": "string"
                },
                "name": {
                  "description": "Target database label, e.g. the organism or proteome id.",
                  "type": "string"
                },
                "organism": {
                  "description": "Organism label used in presence cells (defaults to 'name').",
                  "type": "string"
                },
                "taxid": {
                  "description": "NCBI taxid for the target proteome.",
                  "type": "integer"
                }
              },
              "required": [
                "fasta"
              ],
              "type": "object"
            },
            "type": "array"
          },
          "weak_evalue": {
            "default": 1.0,
            "description": "E-value cutoff below which a call is 'weak' rather than absent.",
            "minimum": 0,
            "type": "number"
          }
        },
        "required": [
          "mode"
        ],
        "type": "object"
      },
      "timeoutMs": 600000,
      "title": "Homolog search and presence matrix"
    },
    {
      "category": "statistics",
      "command": "stats",
      "description": "Statistical association between two numeric columns of a small table. Reports n, Pearson r (with p), Spearman rho (with p), an optional partial correlation controlling for a nuisance variable (control='auto' uses an existing 'core' column; control='core' derives it from core_from, i.e. total_column minus segment_columns), a log-log fit log(y)=a+slope*log(x), per-group correlations, and standardised-residual outliers. group_by='auto' groups identifiers by the prefix before the first '_' or '-'. Always emits CORRELATION_NOT_CAUSATION and PHYLOGENETIC_NON_INDEPENDENCE caveats, plus SMALL_N below the threshold. Row filters: name_prefix, name_suffix, name_contains, name_regex, ids, and per-column value filters.",
      "examples": [
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
        },
        {
          "group_by": "none",
          "table": "tables/measurements.tsv",
          "x": "length",
          "y": "score"
        }
      ],
      "name": "lifesci_stats",
      "parameters": {
        "additionalProperties": false,
        "oneOf": [
          {
            "required": [
              "table"
            ]
          },
          {
            "required": [
              "rows"
            ]
          }
        ],
        "properties": {
          "control": {
            "description": "Column for the partial correlation controlling variable, or the special value 'core'. Default 'auto': use an existing 'core' column if present, else no partial correlation. 'core' without an existing column requires 'core_from' (total_column + segment_columns). Null disables the partial correlation.",
            "type": [
              "string",
              "null"
            ]
          },
          "core_from": {
            "additionalProperties": false,
            "description": "Definition used when control='core': core = total - sum(segment columns).",
            "properties": {
              "segment_columns": {
                "description": "Segment columns subtracted from the total column to give the core value.",
                "items": {
                  "type": "string"
                },
                "type": "array"
              },
              "total_column": {
                "description": "Column holding the full (core + segment) length.",
                "type": "string"
              }
            },
            "type": "object"
          },
          "filter": {
            "additionalProperties": false,
            "description": "Row selection applied before correlation (all listed conditions must match).",
            "properties": {
              "columns": {
                "additionalProperties": true,
                "description": "Column filters: keys are column names, values are a literal or list of allowed literal values (compared as strings).",
                "type": "object"
              },
              "ids": {
                "description": "Keep only these exact ids.",
                "items": {
                  "type": "string"
                },
                "type": "array"
              },
              "name_contains": {
                "description": "Keep only ids containing this substring.",
                "type": "string"
              },
              "name_prefix": {
                "description": "Keep only ids starting with this prefix.",
                "type": "string"
              },
              "name_regex": {
                "description": "Keep only ids matching this Python regular expression.",
                "type": "string"
              },
              "name_suffix": {
                "description": "Keep only ids ending with this suffix.",
                "type": "string"
              }
            },
            "type": "object"
          },
          "group_by": {
            "description": "Column used to split per-group correlations. Default 'auto' groups by the identifier prefix before the first '_' or '-' (lowercased); 'none' disables groups; any other value must be an existing column name.",
            "type": "string"
          },
          "id_column": {
            "description": "Column used as the point identifier/label. Default: JSON object keys, else the first of name/id/key/label/organism present, else row_NNNN.",
            "type": "string"
          },
          "include_points": {
            "default": true,
            "description": "Include the per-point (label, x, y, group, control) table in the payload.",
            "type": "boolean"
          },
          "out": {
            "description": "Optional path to write the statistics payload JSON.",
            "type": "string"
          },
          "outlier_z": {
            "default": 2.0,
            "description": "Absolute standardised-residual cutoff used to label outliers.",
            "maximum": 10,
            "minimum": 0.5,
            "type": "number"
          },
          "rows": {
            "description": "Inline records (alternative to table).",
            "items": {
              "additionalProperties": true,
              "type": "object"
            },
            "type": "array"
          },
          "small_n_threshold": {
            "default": 100,
            "description": "Emit the SMALL_N caveat when the number of valid points is below this (default 100: cross-species correlations with dozens of species are small).",
            "maximum": 10000,
            "minimum": 3,
            "type": "integer"
          },
          "table": {
            "description": "Path to a table: JSON (object-of-records keyed by name, list of records, or {'rows': [...]}), TSV, or CSV. Relative paths resolve against ctx.cwd.",
            "type": "string"
          },
          "x": {
            "description": "Numeric column for the x variable. Log-log fit uses log(x).",
            "type": "string"
          },
          "y": {
            "description": "Numeric column for the y variable. Log-log fit uses log(y).",
            "type": "string"
          }
        },
        "required": [
          "x",
          "y"
        ],
        "type": "object"
      },
      "timeoutMs": 120000,
      "title": "Correlation, partial correlation, and log-log regression"
    },
    {
      "category": "structure",
      "command": "struct.fetch",
      "description": "Fetch protein structure models/structures reproducibly. AlphaFold DB entries come with their model version and file URLs; when AFDB has no model (common for TrEMBL entries) the command attempts an ESMFold single-sequence prediction and then, if that also fails, reports an explicit 'no model' verdict in unavailable[] with warnings - an unavailable model is NOT evidence of absence. RCSB PDB mmCIF files and EMDB density maps (large; warning) are also supported. Every written file carries a SHA-256 recorded in provenance, and the model version/release is returned for each available structure. Caveats: AlphaFold/ESMFold models are predictions, not experimental structures; ESMFold is limited to short sequences (HTTP 413 for very long proteins) and can time out; per-residue confidence (pLDDT) is not evaluated here.",
      "examples": [
        {
          "accessions": [
            "P69905"
          ]
        },
        {
          "out": "structures",
          "pdb_ids": [
            "1UBQ"
          ]
        }
      ],
      "name": "lifesci_struct_fetch",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "accessions": {
            "description": "UniProt accessions whose AlphaFold DB models to fetch. AFDB has no model for many TrEMBL entries; those entries go through the ESMFold fallback (when enabled) and, if that fails too, are reported in unavailable[] with an explicit 'no model' verdict.",
            "items": {
              "minLength": 1,
              "type": "string"
            },
            "type": "array"
          },
          "afdb_format": {
            "default": "pdb",
            "description": "File format for AlphaFold DB model downloads (pdb or cif).",
            "enum": [
              "pdb",
              "cif"
            ],
            "type": "string"
          },
          "emdb_ids": {
            "description": "EMDB map IDs (e.g. EMD-1234). Maps are large; they are written to 'out' only.",
            "items": {
              "type": "string"
            },
            "type": "array"
          },
          "esmfold_fallback": {
            "default": true,
            "description": "When an AlphaFold DB model is unavailable, fetch the UniProt sequence and attempt an ESMFold prediction (single-sequence, not an experimentally validated structure). The fallback attempt and its outcome are always reported; ESMFold returns HTTP 413 for very long proteins and can time out (504).",
            "type": "boolean"
          },
          "out": {
            "description": "Output directory for downloaded model/structure files (default <cache-dir>/out/struct.fetch). Relative paths resolve against the working directory. Every file gets a recorded SHA-256.",
            "type": "string"
          },
          "pdb_format": {
            "default": "cif",
            "description": "File format for RCSB PDB downloads (mmCIF is the canonical format).",
            "enum": [
              "cif",
              "pdb"
            ],
            "type": "string"
          },
          "pdb_ids": {
            "description": "RCSB PDB IDs (e.g. 1UBQ) to download as experimental structures.",
            "items": {
              "minLength": 4,
              "type": "string"
            },
            "type": "array"
          }
        },
        "type": "object"
      },
      "timeoutMs": 600000,
      "title": "Fetch structure models"
    },
    {
      "category": "structure",
      "command": "struct.search",
      "description": "Structural-similarity search with explicit interpretation limits. Modes: 'panel' runs the local Foldseek binary on a controlled panel and calibrates the background with negative controls (unrelated proteins of the same fold class); only scores above control_max_tm + margin are interpretable. 'web' runs the Foldseek web API (AFDB50/AFDB-SwissProt/PDB100; only multipart PDB upload works). 'from_table' replays recorded hit tables offline and optionally checks caller-supplied sanity anchors (expected_anchors), warning with SANITY_ANCHOR_MISSING when one is not recovered. Always reported: TM-scores are query-normalised (fragment/short queries score higher), result lists are capped (AFDB50 top ~1000 per database), and E-values are database-size dependent, so E-values must never be compared across databases. Structural similarity supports 'closest structural neighbour' but is not automatic orthology; use reciprocal searches and domain/family evidence as the arbiter.",
      "examples": [
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
        },
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
        },
        {
          "databases": [
            "afdb50"
          ],
          "mode": "web",
          "pdb_accessions": [
            "P69905"
          ]
        }
      ],
      "name": "lifesci_struct_search",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "binary": {
            "description": "Explicit Foldseek binary path (default: $LIFESCI_FOLDSEEK, tools/bin, then PATH).",
            "type": "string"
          },
          "controls": {
            "description": "panel mode: negative-control structures of the same fold class (unrelated proteins). They are searched against the same panel; the maximum control TM is the background and hits must exceed control_max_tm + margin to be interpretable.",
            "items": {
              "oneOf": [
                {
                  "type": "string"
                },
                {
                  "additionalProperties": false,
                  "properties": {
                    "id": {
                      "type": "string"
                    },
                    "path": {
                      "type": "string"
                    }
                  },
                  "required": [
                    "path"
                  ],
                  "type": "object"
                }
              ]
            },
            "type": "array"
          },
          "databases": {
            "default": [
              "afdb50"
            ],
            "description": "web mode: Foldseek databases to search. AFDB50 is the global sensitive AFDB search.",
            "items": {
              "enum": [
                "afdb50",
                "afdb-swissprot",
                "pdb100"
              ],
              "type": "string"
            },
            "type": "array"
          },
          "db_metadata": {
            "description": "Optional extra database/release metadata to record with the result.",
            "type": "object"
          },
          "e": {
            "default": 100.0,
            "description": "panel mode: Foldseek E-value reporting threshold.",
            "minimum": 0,
            "type": "number"
          },
          "expected_anchors": {
            "description": "Anchors that must be recovered for the search to be considered sane. There are no built-in anchors: supply the expected target(s) explicitly.",
            "items": {
              "additionalProperties": false,
              "properties": {
                "family_hint": {
                  "description": "Alternative to target_accession: '|'-separated substrings that must appear in a top hit's description/organism.",
                  "type": "string"
                },
                "max_rank": {
                  "description": "The hit must be found at or above this rank.",
                  "minimum": 1,
                  "type": "integer"
                },
                "min_prob": {
                  "description": "Minimum Foldseek homology probability the hit must reach.",
                  "type": "number"
                },
                "min_tm": {
                  "description": "Minimum TM-score the hit must reach.",
                  "type": "number"
                },
                "query": {
                  "description": "Query label/accession this anchor applies to (default: any query).",
                  "type": "string"
                },
                "required": {
                  "default": true,
                  "description": "Anchor failure is reported as SANITY_ANCHOR_MISSING when required.",
                  "type": "boolean"
                },
                "target_accession": {
                  "description": "Accession that must be recovered.",
                  "type": "string"
                }
              },
              "type": "object"
            },
            "type": "array"
          },
          "fmt": {
            "default": "query,target,alntmscore,qtmscore,ttmscore,lddt,evalue,qcov,tcov,alnlen",
            "description": "panel mode: Foldseek --format-output string.",
            "type": "string"
          },
          "hits": {
            "description": "from_table mode: inline recorded hits instead of files. Each row may carry query/db/rank/target_accession/tm_score/prob/evalue/... (web format) or query/target/alntmscore/... (panel format).",
            "items": {
              "type": "object"
            },
            "type": "array"
          },
          "margin": {
            "default": 0.05,
            "description": "panel mode: margin above the control maximum before a TM score is interpretable.",
            "maximum": 1,
            "minimum": 0,
            "type": "number"
          },
          "max_wait": {
            "default": 600,
            "description": "web mode: maximum seconds to wait for a Foldseek web job.",
            "minimum": 10,
            "type": "number"
          },
          "min_control_coverage": {
            "default": 0.3,
            "description": "panel mode: minimum query coverage (qcov) for a negative-control alignment to count towards the background. Foldseek can give a high alignment TM for a 10-residue fragment; such fragments are not a meaningful background.",
            "maximum": 1,
            "minimum": 0,
            "type": "number"
          },
          "min_hit_coverage": {
            "default": 0.3,
            "description": "panel mode: minimum query coverage (qcov) for a query hit to be interpretable. Hits below this are reported but flagged as low-coverage, not called homologous.",
            "maximum": 1,
            "minimum": 0,
            "type": "number"
          },
          "mode": {
            "description": "Search backend: 'panel' = local Foldseek on a controlled panel with negative controls; 'web' = Foldseek web API against AFDB50/SwissProt/PDB100; 'from_table' = offline replay of recorded hit tables with sanity-anchor checks.",
            "enum": [
              "panel",
              "web",
              "from_table"
            ],
            "type": "string"
          },
          "out": {
            "description": "Optional directory: combined hits TSV + search summary JSON + anchor report are written here.",
            "type": "string"
          },
          "pdb_accessions": {
            "description": "web mode: UniProt accessions whose AlphaFold DB PDB models are used as queries.",
            "items": {
              "type": "string"
            },
            "type": "array"
          },
          "pdb_paths": {
            "description": "web mode: local PDB model files uploaded as queries (only PDB upload works; FASTA/JSON fail).",
            "items": {
              "type": "string"
            },
            "type": "array"
          },
          "poll_interval": {
            "default": 5,
            "description": "web mode: seconds between ticket polls.",
            "minimum": 1,
            "type": "number"
          },
          "queries": {
            "description": "panel mode: query structures as {\"id\", \"path\"} objects or path strings.",
            "items": {
              "oneOf": [
                {
                  "type": "string"
                },
                {
                  "additionalProperties": false,
                  "properties": {
                    "id": {
                      "description": "Query label (defaults to the file stem).",
                      "type": "string"
                    },
                    "path": {
                      "description": "Local structure file (PDB/mmCIF).",
                      "type": "string"
                    }
                  },
                  "required": [
                    "path"
                  ],
                  "type": "object"
                }
              ]
            },
            "type": "array"
          },
          "tables": {
            "description": "from_table mode: recorded hit table(s) — path strings or {\"path\", \"query\", \"database\", \"format\"}.",
            "oneOf": [
              {
                "type": "string"
              },
              {
                "items": {},
                "type": "array"
              }
            ]
          },
          "targets": {
            "description": "panel mode: the target panel — a directory path, {\"dir\": path}, or a list of structure paths. Candidate homologues and negative controls should be in the panel.",
            "oneOf": [
              {
                "type": "string"
              },
              {
                "additionalProperties": false,
                "properties": {
                  "dir": {
                    "type": "string"
                  }
                },
                "type": "object"
              },
              {
                "items": {
                  "type": "string"
                },
                "type": "array"
              }
            ]
          },
          "taxfilter": {
            "description": "web mode: optional NCBI taxonomy filter (applied only when set).",
            "type": "string"
          },
          "threads": {
            "default": 4,
            "description": "panel mode: Foldseek threads.",
            "maximum": 64,
            "minimum": 1,
            "type": "integer"
          }
        },
        "required": [
          "mode"
        ],
        "type": "object"
      },
      "timeoutMs": 600000,
      "title": "Foldseek structural search"
    },
    {
      "category": "structure",
      "command": "struct.superpose",
      "description": "Structural comparison with two modes. kabsch: superpose structures on shared alignment columns (from colmap residue→column maps), optionally excluding spans such as a variable region; returns per-structure rotation/translation into the reference frame, core RMSD and local RMSD near the focus spans, plus absent columns. Warns FEW_CORE_COLUMNS and raises NO_SHARED_COLUMNS when too few columns are shared. foldseek: pairwise structural alignment via the local Foldseek binary, reporting TM/LDDT/e-value/coverage per pair. Always treats TM as query-length normalised and E-values as database-size dependent: run reciprocal searches and never compare E-values across databases. Trimmed structures are valid inputs; use the preserved residue numbering and colmap.",
      "examples": [
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
      ],
      "name": "lifesci_struct_superpose",
      "parameters": {
        "additionalProperties": false,
        "properties": {
          "binary": {
            "description": "foldseek: explicit Foldseek binary path (default: LIFESCI_FOLDSEEK / tools/bin / $PATH).",
            "type": "string"
          },
          "core_columns": {
            "description": "kabsch: explicit alignment columns to use as core (intersected with the shared/modelled set).",
            "items": {
              "type": "integer"
            },
            "type": "array"
          },
          "core_spans": {
            "description": "kabsch: restrict the core to these alignment-column spans (inclusive).",
            "items": {
              "items": {
                "type": "integer"
              },
              "maxItems": 2,
              "minItems": 2,
              "type": "array"
            },
            "type": "array"
          },
          "e": {
            "default": 100,
            "description": "foldseek: E-value cutoff for the local search (E-values are database-size dependent).",
            "maximum": 10000,
            "minimum": 0,
            "type": "number"
          },
          "exclude_spans": {
            "description": "kabsch: alignment-column spans [[start,end], ...] excluded from the core (e.g. a variable region, so the fit is not driven by it).",
            "items": {
              "items": {
                "type": "integer"
              },
              "maxItems": 2,
              "minItems": 2,
              "type": "array"
            },
            "type": "array"
          },
          "focus_spans": {
            "description": "kabsch: spans defining the insertion-site neighbourhood for local RMSD (local core columns are those within local_window columns of any focus span). Default: exclude_spans.",
            "items": {
              "items": {
                "type": "integer"
              },
              "maxItems": 2,
              "minItems": 2,
              "type": "array"
            },
            "type": "array"
          },
          "local_window": {
            "default": 600,
            "description": "kabsch: half-window in alignment columns around focus_spans used for the local RMSD.",
            "maximum": 100000,
            "minimum": 0,
            "type": "integer"
          },
          "mode": {
            "default": "kabsch",
            "description": "'kabsch': column-based superposition (requires a colmap per structure; reports transforms and core/local RMSD). 'foldseek': pairwise TM/LDDT alignment using the local Foldseek binary.",
            "enum": [
              "kabsch",
              "foldseek"
            ],
            "type": "string"
          },
          "out": {
            "description": "Optional directory for Foldseek intermediates and/or the payload JSON (a .json suffix writes JSON).",
            "type": "string"
          },
          "reference": {
            "description": "kabsch: id of the reference structure (default: first structure). Transforms map others into its frame.",
            "type": "string"
          },
          "structures": {
            "description": "Structures to compare; ids must be unique.",
            "items": {
              "additionalProperties": false,
              "properties": {
                "colmap": {
                  "description": "Residue→alignment-column map: colmap JSON path or inline object. Required in kabsch mode, ignored in foldseek mode.",
                  "oneOf": [
                    {
                      "type": "string"
                    },
                    {
                      "additionalProperties": true,
                      "type": "object"
                    }
                  ]
                },
                "id": {
                  "description": "Unique structure/model identifier used in the output.",
                  "type": "string"
                },
                "path": {
                  "description": "mmCIF/PDB file (gzip allowed; trimmed structures work).",
                  "type": "string"
                }
              },
              "required": [
                "id",
                "path"
              ],
              "type": "object"
            },
            "minItems": 2,
            "type": "array"
          },
          "threads": {
            "default": 4,
            "description": "foldseek: CPU threads for the local search.",
            "maximum": 64,
            "minimum": 1,
            "type": "integer"
          }
        },
        "required": [
          "structures"
        ],
        "type": "object"
      },
      "timeoutMs": 600000,
      "title": "Superpose structures (Kabsch on alignment columns, or Foldseek TM)"
    }
  ],
  "version": "0.2.0"
} as const;
