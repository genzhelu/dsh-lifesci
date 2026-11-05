# Remote execution over SSH (optional, opt-in, off by default)

Some workflows need more compute than a workstation has: structure prediction, HH-suite
searches, large HMMER/MMseqs2 jobs. The `lifesci.remote` command can offload such jobs to
machines you control over SSH. It is **never required**: local and offline paths work without
it, and offline tests never touch a network or a remote host.

## Enabling

Remote execution stays disabled until hosts are explicitly configured in the user's
environment (never in the repository):

```bash
export LIFESCI_REMOTE_HOSTS='{
  "hosts": {
    "gpu01": {
      "host": "gpu01.example.edu",
      "user": "analyst",
      "port": 22,
      "key_path": "~/.ssh/id_ed25519",
      "work_dir": "/scratch/$USER/lifesci",
      "tool_paths": {
        "hhsuite": "/opt/hhsuite",
        "hmmer_bin": "/opt/hmmer/bin",
        "alphafold_data": "/data/alphafold"
      },
      "allowed_commands": ["hmmsearch", "jackhmmer", "hhblits", "colabfold_batch", "nvidia-smi"]
    },
    "gpu02": {
      "host": "gpu02.example.edu",
      "user": "analyst",
      "port": 22,
      "key_path": "~/.ssh/id_ed25519",
      "work_dir": "/tmp/lifesci-$USER",
      "tool_paths": {"hhsuite": "/opt/hhsuite"},
      "allowed_commands": ["hhblits", "nvidia-smi"]
    }
  }
}'
```

Or point `LIFESCI_REMOTE_CONFIG` at a JSON file with the same structure (do not commit it).

Each host entry supports:

| field | meaning |
|---|---|
| `host` | SSH destination (hostname or IP; required) |
| `user` | SSH user (optional; defaults to the local user) |
| `port` | SSH port (optional; defaults to 22) |
| `key_path` | private key path passed to `ssh -i`; never returned or logged |
| `work_dir` | remote working directory for jobs (optional) |
| `tool_paths` | `{label: absolute_path}` map of tool bundles/data available on the host; informational, never executed as a shell fragment |
| `allowed_commands` | argv-prefix allow-list of executables that may run (recommended; deny-by-default when set) |
| `label` | optional human-readable label shown by the host listing |

The `tool_paths` map is machine-specific: use absolute paths that exist on the remote host,
e.g. `{"hhsuite": "/opt/hhsuite"}` with `/opt/hhsuite/bin/hhsearch` as the executable, and
database directories such as `/data/hhsuite/db`.

## Using

```bash
# list configured hosts (no secrets in output)
.venv/bin/python -m lifesci remote --input '{"action": "hosts"}'

# dry-run: show exactly what would run, without connecting
.venv/bin/python -m lifesci remote --input \
  '{"action": "run", "host": "gpu01", "argv": ["nvidia-smi"], "dry_run": true}'

# real execution requires explicit allow=true (plus the host allow-list)
.venv/bin/python -m lifesci remote --input \
  '{"action": "run", "host": "gpu01", "argv": ["/opt/hhsuite/bin/hhsearch", "-h"], "allow": true}'
```

Rules enforced by the adapter:

- No hosts configured → `REMOTE_DISABLED` error (the command never falls back to a hard-coded
  host).
- `argv` must be a list (never a shell string); the command is executed as `ssh host -- argv...`
  using argument arrays, no shell interpolation.
- If a host declares `allowed_commands`, only those executables may run (checked on `argv[0]`).
- Jobs are opt-in: `allow: true` is required for a real run; `dry_run` is always allowed.
- Provenance records host alias, argv, start/end time, exit code, and the remote tool version
  probe; it never records key paths or key material.
- Timeouts are enforced locally (`timeout` input, default 600 s); the child `ssh` process is
  killed on abort/timeout.
- Remote outputs are copied back into the content-addressed cache by the caller and logged like
  any other provenance record; no model is silently trained or updated.
