# Installing the `lifesci` plugin into DeepSeek Harness

This document is the procedure for registering this repository as a **host plugin** (`bundle`)
in a DeepSeek Harness profile, plus verification and troubleshooting. It applies to
`@genzhelu/dsh-lifesci` v0.2.0 (source repository:
`https://github.com/genzhelu/dsh-lifesci`).

The plugin is a Cordis **host plugin bundle**: `package.json` declares
`"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }`, and the patch inserts one host row
(`id: lifesci`, `name: '@genzhelu/dsh-lifesci'`). The package is linked into the profile and the
bundle is added to the profile's bundle list; the Loader then mounts the plugin, which registers
the `lifesci_*` agent tools.

## 0. Prerequisites

- Python ≥ 3.11 with a virtual environment containing `requirements.txt`
  (`.venv/bin/python` in the repository root is picked up automatically).
- Node ≥ 20 and a built `dist/` directory (`npm run build`).
- A DeepSeek Harness installation with a profile (the harness must expose the `tools` service;
  every current profile does).
- `plugin_manager` tool available in the agent session (it ships with the harness).

## 1. Build and self-test the plugin (outside the harness)

```bash
git clone https://github.com/genzhelu/dsh-lifesci.git
cd dsh-lifesci

python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

npm install --cache=./.npm-cache --no-audit --no-fund
npm run build                      # generates src/tool-specs.ts, then compiles to dist/

# Science-layer smoke test (no harness involved):
.venv/bin/python -m lifesci doctor --input '{"network": false}'
.venv/bin/python -m lifesci --list | head -20
```

`npm run build` requires the Python interpreter because the model-facing tool specs are
generated from the Python command registry (single source of truth). If `python3` on PATH is not
the venv, point the build at it: `LIFESCI_PYTHON=$PWD/.venv/bin/python npm run build`.

A prebuilt `dist/` is committed so the repository can also be installed directly from Git; rebuild
it after changing `src/` or the Python command registry.

## 2. Install into the harness profile (recommended path)

From an **agent session** in this harness, call the `plugin_manager` tool:

```json
{
  "action": "install_bundle",
  "target": "/absolute/path/to/dsh-lifesci-plugin"
}
```

This performs the package installation (`pnpm add <path>` in the profile directory) and the
bundle selection (adds `@genzhelu/dsh-lifesci` to the profile's `dsh.profile.bundles`). Do **not**
hand-edit the profile's `package.json` or `cordis.patch.yml`; `install_bundle` owns those steps,
and profile writes outside the workspace need approval.

The action requires approval (the tool call asks through your configured answerer). Read its
result:
- `application: applied` — the row was mounted (new bundles can activate through HMR).
- `restart-required` — restart the harness session/profile for the fresh module generation.
- `failed` — read the diagnostics, then use §6.

> A local directory install **links** the checkout: the profile resolves `dist/index.js` live
> from this repository. Rebuilding `dist/` in place requires a harness restart before the new
> module generation is loaded.

### Install directly from GitHub (optional)

`plugin_manager` also accepts a Git address as the installation spec:

```json
{
  "action": "install_bundle",
  "target": "git+https://github.com/genzhelu/dsh-lifesci.git"
}
```

The committed `dist/` makes this work without a Node build step inside the profile. Two caveats:

- A profile install has no `<package>/.venv`, so create the Python environment separately
  (`python3.11 -m venv ~/.local/share/lifesci-venv && …/pip install -r requirements.txt`) and set
  the bundle's `pythonPath` in the profile patch (or `$LIFESCI_PYTHON`) to that interpreter.
- A **private** GitHub repository requires Git credentials configured for the profile's pnpm
  invocation (HTTPS token or SSH key); otherwise pnpm cannot fetch the package. A public
  repository installs without credentials.

Select the `lifesci` bundle after installation if `install_bundle` reports it installed but not
selected, then restart the harness.

### Alternative: `dsh plugin` CLI

The CLI forwards to `pnpm` *inside the profile* and adds the package dependency, but it does not
select the bundle. Use it only if you also add the bundle to the profile's bundle list (which
`install_bundle` normally does):

```bash
dsh plugin --profile <profile-name> add /absolute/path/to/dsh-lifesci-plugin
# then ensure "@genzhelu/dsh-lifesci" is listed in the profile's dsh.profile.bundles
```

Prefer `install_bundle`; it keeps installation and activation consistent.

## 3. Verify

1. **Inventory** — query the plugin inventory (Plugin Manager / Settings → Plugins in the Web UI,
   or `plugin_manager action: list_plugins`). The bundle `@genzhelu/dsh-lifesci` must appear, and
   its row `lifesci` must be active (not `failed`/`overridden`).
2. **Tools are exposed** — in a new or refreshed agent session, the tool list contains
   `lifesci_doctor`, `lifesci_seq_fetch`, `lifesci_struct_search`, … (17 tools; the exact set is
   generated from `python -m lifesci --list`). `cordis_inspect_query` on the Tool provider can
   confirm registration without browser control.
3. **Run it** — ask the agent: `lifesci_doctor` with `{"network": true}`. It returns the same
   JSON payload as the CLI doctor; `data.status` should be `ok`, `ok-with-warnings`, or
   `degraded` (see caveats below), and `data.python.executable` should point at a Python with
   all required packages installed.
4. **End-to-end** — `lifesci_seq_fetch` with `{"accessions": ["P69905"]}`, then the offline
   equivalent (run the CLI with `--offline`, or set the bridge config `offline: true`) to confirm
   cache replay.

If the tools do not appear, see §6.

## 4. Optional configuration

The bundle row accepts a `config` object (the plugin's `apply(ctx, config)` reads it). To override
defaults, install with an empty config first, then override the row in the **user patch layer**
(`$DSH_HOME/profiles/<profile>/cordis.patch.yml`) through the normal configuration workflow —
an `id: lifesci` entry with `config:` replaces the row's config:

```yaml
- id: lifesci
  name: '@genzhelu/dsh-lifesci'
  config:
    pythonPath: /path/to/.venv/bin/python
    cacheDir: /path/to/analysis/.lifesci-cache
    offline: false
    defaultTimeoutMs: 300000
    enabledTools: ["lifesci_doctor", "lifesci_seq_fetch", "lifesci_struct_search"]
```

| field | default | meaning |
|---|---|---|
| `pythonPath` | `$LIFESCI_PYTHON` → `<repo>/.venv/bin/python` → `python3` | science-layer interpreter |
| `cacheDir` | `<session cwd>/.lifesci-cache` | content-addressed cache + provenance log |
| `logPath` | `<cacheDir>/provenance.jsonl` | JSONL provenance log |
| `offline` | `false` | cache-only mode for every tool call |
| `defaultTimeoutMs` | `180000` | per-tool subprocess timeout |
| `enabledTools` | all | allow-list of registered tool names |
| `extraEnv` | `{}` | environment variables passed to the Python subprocess |

Environment variables read by the science layer: `LIFESCI_CACHE_DIR`, `LIFESCI_LOG`,
`LIFESCI_OFFLINE`, `LIFESCI_DRY_RUN`, `LIFESCI_HTTP_TIMEOUT`, `LIFESCI_HTTP_RETRIES`,
`LIFESCI_CACHE_TTL`, `LIFESCI_CONTACT_EMAIL`, `LIFESCI_FOLDSEEK`, `LIFESCI_PYMOL`,
`LIFESCI_DATASETS`, `LIFESCI_REMOTE_HOSTS`.

## 5. Optional external tools

| tool | needed for | install |
|---|---|---|
| Foldseek (pinned static build) | `struct.search` panel mode, foldseek superposition | `scripts/install_foldseek.sh` (downloads the pinned static build, verifies SHA-256, installs to `tools/bin/`) |
| PyMOL | `figures` session build/validation | system package / conda; absent → validation reported `SKIPPED` (never faked) |
| ssh | `remote` command | system OpenSSH; remote execution is opt-in and configured via `LIFESCI_REMOTE_HOSTS` |

`lifesci_doctor` reports each tool and portal state; nothing is silently downloaded at runtime.

## 6. Troubleshooting

| symptom | cause / fix |
|---|---|
| `install_bundle` → `failed` | Read `diagnostics`; usually a missing `dist/index.js` (run `npm run build`) or an invalid `cordis.patch.yml`. |
| bundle installed but tools absent | Harness module cache: restart the harness (row activation through HMR does not always pick up a fresh package generation). Check the row is not `overridden`/`disabled` in bundle details. |
| tools present but every call fails with python error | `pythonPath` wrong or venv missing deps: run `.venv/bin/python -m lifesci doctor --offline`; set `pythonPath` or `LIFESCI_PYTHON`. |
| tool call returns `[OFFLINE_CACHE_MISS]` | `offline: true` is set and the payload was never cached; run once with `offline: false` (online) to fill the cache, then retry offline. |
| `[TOOL_MISSING]` for Foldseek/PyMOL | see §5; commands degrade gracefully where possible. |
| `lifesci_doctor` shows `degraded` | One or more optional portals is unreachable; check `data.network` and `data.checks`. Core commands that use the cache/offline still work. |
| plugin row `failed` after upgrade | Rebuild `dist/` and restart; a linked local package serves the rebuilt JS only on a fresh module generation. |
| direct Git install cannot fetch the repository | The repository is private: configure Git credentials for the profile's pnpm (HTTPS token or SSH key), or install from a local clone instead. |
| tools fail after a direct Git install | No `<package>/.venv` exists for a profile install: create a venv, install `requirements.txt`, and set the bundle `pythonPath` / `$LIFESCI_PYTHON`. |

## 7. Upgrade

For a linked checkout install:

```bash
cd /path/to/dsh-lifesci
git pull --ff-only
.venv/bin/pip install -r requirements.txt
npm install --no-audit --no-fund
npm run build
# restart the harness (or the profile) so the fresh module generation loads
```

For a direct Git install, re-run `plugin_manager action: install_bundle` with the same target after
removing the installed bundle, or restart the harness after the profile fetches the new revision.

## 8. Uninstall

Use `plugin_manager action: remove_bundle target: '@genzhelu/dsh-lifesci'` (removes the bundle
from the profile and uninstalls the linked package). Remove caches/logs manually only if desired:
`<analysis>/.lifesci-cache/`.
