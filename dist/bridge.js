/**
 * Python bridge: spawn the `lifesci` CLI, parse its single JSON envelope.
 *
 * The TypeScript plugin is a thin, zero-dependency shell around the Python
 * science layer. Every model-facing tool call becomes one child process:
 *
 * ```
 * <python> -m lifesci <command> --input <json> --cache-dir <dir> --log <path>
 *          [--offline] [--dry-run]
 * ```
 *
 * The child prints exactly one JSON envelope to stdout (diagnostics go to
 * stderr) and exits 0 for `ok:true`, non-zero for `ok:false`; the envelope is
 * authoritative, the exit code is only a backstop. This module keeps the
 * decision logic in pure helpers ({@link resolvePython},
 * {@link buildInvocation}, {@link parseEnvelope}, {@link formatEnvelopeError})
 * so it is fully unit-testable without a Python environment, and confines the
 * process I/O to {@link runProcess}/{@link runTool}.
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import * as nodePath from "node:path";
import { fileURLToPath } from "node:url";
import { DEFAULT_TIMEOUT_MS, expandHome, resolveAgainst } from "./config.js";
/** Cap on buffered child output; a lifesci payload larger than this is a bug. */
export const MAX_BUFFER_BYTES = 64 * 1024 * 1024;
/** Default cache directory name created inside the session workspace. */
export const DEFAULT_CACHE_DIR_NAME = ".lifesci-cache";
/** Grace period between SIGTERM and SIGKILL when a call is aborted/timed out. */
const KILL_GRACE_MS = 2_000;
/** Error thrown by the Python bridge; `code` is stable for callers/tests. */
export class LifesciBridgeError extends Error {
    /** Stable machine-readable error code (`ENOENT`, `TIMEOUT`, `ABORTED`, `BAD_ENVELOPE`, ...). */
    code;
    constructor(message, options = {}) {
        super(message, options.cause === undefined ? undefined : { cause: options.cause });
        this.name = "LifesciBridgeError";
        this.code = options.code;
    }
}
/** Extract a printable message from an unknown thrown value. */
export function errorMessage(error) {
    if (error instanceof Error)
        return error.message;
    return String(error);
}
/** Truncate captured stderr for inclusion in an error message. */
export function stderrTail(stderr, maxChars = 2_000) {
    const text = stderr.trim();
    if (text.length === 0)
        return "";
    return text.length > maxChars ? `…${text.slice(-maxChars)}` : text;
}
function formatStderr(stderr) {
    const tail = stderrTail(stderr);
    return tail.length === 0 ? "" : `\n--- stderr (tail) ---\n${tail}`;
}
/** Truncate captured stdout for inclusion in a parse error. */
export function stdoutSnippet(stdout, maxChars = 2_000) {
    const text = stdout.trim();
    if (text.length === 0)
        return "(empty)";
    return text.length > maxChars ? `${text.slice(0, maxChars)}…` : text;
}
/** Root of this package, resolved from the compiled module location (`dist/bridge.js` → package root). */
export function repoRootFromModule(moduleUrl = import.meta.url) {
    return nodePath.dirname(nodePath.dirname(fileURLToPath(moduleUrl)));
}
/** Package root of the running plugin build. */
export const REPO_ROOT = repoRootFromModule();
/**
 * Resolve a configured executable reference.
 *
 * Bare command names (`python3`) pass through so the OS `PATH` resolves them;
 * absolute paths pass through; anything containing a separator is resolved
 * against the package root, matching how a profile config is authored.
 */
function resolveExecutableReference(repoRoot, value) {
    const expanded = expandHome(value.trim());
    if (nodePath.isAbsolute(expanded))
        return expanded;
    if (expanded.includes("/") || expanded.includes("\\")) {
        return resolveAgainst(repoRoot, expanded);
    }
    return expanded;
}
/**
 * Pick the Python interpreter, in documented priority order:
 *
 * 1. explicit `config.pythonPath`;
 * 2. `$LIFESCI_PYTHON`;
 * 3. the bundled source-checkout venv (`<repo>/.venv/bin/python`);
 * 4. `python3` (a bare command resolved through `PATH`).
 *
 * @param options - injectable environment/filesystem facts.
 * @returns The chosen interpreter and where it came from.
 */
export function resolvePython(options = {}) {
    const repoRoot = options.repoRoot ?? REPO_ROOT;
    const platform = options.platform ?? process.platform;
    const exists = options.exists ?? existsSync;
    if (options.pythonPath !== undefined && options.pythonPath.trim().length > 0) {
        return { python: resolveExecutableReference(repoRoot, options.pythonPath), source: "config" };
    }
    const envPython = (options.env ?? process.env).LIFESCI_PYTHON;
    if (typeof envPython === "string" && envPython.trim().length > 0) {
        return { python: resolveExecutableReference(repoRoot, envPython), source: "env" };
    }
    const venvCandidates = platform === "win32"
        ? [
            nodePath.join(repoRoot, ".venv", "Scripts", "python.exe"),
            nodePath.join(repoRoot, ".venv", "bin", "python"),
        ]
        : [
            nodePath.join(repoRoot, ".venv", "bin", "python"),
            nodePath.join(repoRoot, ".venv", "bin", "python3"),
        ];
    for (const candidate of venvCandidates) {
        if (exists(candidate))
            return { python: candidate, source: "venv" };
    }
    return { python: platform === "win32" ? "python" : "python3", source: "path" };
}
function serializeArguments(spec, args) {
    try {
        const serialized = JSON.stringify(args ?? {});
        return serialized === undefined ? "{}" : serialized;
    }
    catch (error) {
        throw new LifesciBridgeError(`lifesci ${spec.command}: input arguments are not JSON-serializable: ${errorMessage(error)}`, { code: "BAD_INPUT", cause: error });
    }
}
/**
 * Build the child-process plan for one command invocation.
 *
 * Pure apart from the injectable interpreter existence probe; this is the
 * function the unit tests pin down (argument order, `PYTHONPATH`, cache/log
 * defaults, offline/dry-run flags, timeout precedence, extra environment).
 *
 * @param options - spec, resolved config, and injectable environment facts.
 * @returns A frozen invocation plan.
 */
export function buildInvocation(options) {
    const repoRoot = options.repoRoot ?? REPO_ROOT;
    const platform = options.platform ?? process.platform;
    const exists = options.exists ?? existsSync;
    const cwd = nodePath.resolve(options.cwd ?? process.cwd());
    const pythonPackageDir = options.config.pythonPackageDir
        ? resolveAgainst(repoRoot, options.config.pythonPackageDir)
        : nodePath.join(repoRoot, "python");
    const cacheDir = options.config.cacheDir
        ? resolveAgainst(cwd, options.config.cacheDir)
        : nodePath.join(cwd, DEFAULT_CACHE_DIR_NAME);
    const logPath = options.config.logPath
        ? resolveAgainst(cwd, options.config.logPath)
        : nodePath.join(cacheDir, "provenance.jsonl");
    const { python, source } = resolvePython({
        pythonPath: options.config.pythonPath,
        env: options.env,
        repoRoot,
        platform,
        exists,
    });
    const timeoutMs = pickTimeoutMs(options.spec.timeoutMs, options.config.defaultTimeoutMs);
    const args = [
        "-m",
        "lifesci",
        options.spec.command,
        "--input",
        serializeArguments(options.spec, options.args),
        "--cache-dir",
        cacheDir,
        "--log",
        logPath,
    ];
    if (options.config.offline)
        args.push("--offline");
    if (options.config.dryRun)
        args.push("--dry-run");
    const env = {};
    for (const [key, value] of Object.entries(options.env ?? process.env)) {
        if (typeof value === "string")
            env[key] = value;
    }
    const pythonPaths = [pythonPackageDir];
    if (typeof env.PYTHONPATH === "string" && env.PYTHONPATH.trim().length > 0) {
        pythonPaths.push(env.PYTHONPATH);
    }
    env.PYTHONPATH = pythonPaths.join(nodePath.delimiter);
    if (env.PYTHONIOENCODING === undefined)
        env.PYTHONIOENCODING = "utf-8";
    // Explicit plugin config is the operator's last word over inherited environment.
    for (const [key, value] of Object.entries(options.config.extraEnv))
        env[key] = value;
    return {
        command: python,
        args,
        cwd,
        env,
        timeoutMs,
        pythonSource: source,
        cacheDir,
        logPath,
        pythonPackageDir,
    };
}
function pickTimeoutMs(specTimeoutMs, configTimeoutMs) {
    if (typeof specTimeoutMs === "number" && Number.isFinite(specTimeoutMs) && specTimeoutMs > 0) {
        return Math.floor(specTimeoutMs);
    }
    if (Number.isFinite(configTimeoutMs) && configTimeoutMs > 0)
        return Math.floor(configTimeoutMs);
    return DEFAULT_TIMEOUT_MS;
}
function isRecord(value) {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}
/**
 * Parse the single JSON envelope from a command's stdout.
 *
 * Tolerates leading noise (some wrappers prepend a line) by retrying from the
 * first `{`, but never invents success: anything that is not exactly one JSON
 * object with a boolean `ok` is an error that carries the stdout snippet and a
 * stderr tail for diagnosis.
 *
 * @param stdout - captured standard output.
 * @param stderr - captured standard error (used only in diagnostics).
 * @returns The parsed envelope.
 * @throws {LifesciBridgeError} when stdout does not contain a valid envelope.
 */
export function parseEnvelope(stdout, stderr = "") {
    const text = stdout.trim();
    if (text.length === 0) {
        throw new LifesciBridgeError(`lifesci python command produced no JSON envelope on stdout${formatStderr(stderr)}`, { code: "NO_OUTPUT" });
    }
    let parsed;
    let failure;
    try {
        parsed = JSON.parse(text);
    }
    catch (error) {
        failure = error;
        const start = text.indexOf("{");
        if (start >= 0) {
            try {
                parsed = JSON.parse(text.slice(start));
                failure = undefined;
            }
            catch (inner) {
                failure = inner;
            }
        }
    }
    if (failure !== undefined) {
        throw new LifesciBridgeError(`lifesci python command did not return a JSON envelope on stdout (${errorMessage(failure)}).\n` +
            `--- stdout ---\n${stdoutSnippet(text)}${formatStderr(stderr)}`, { code: "BAD_ENVELOPE" });
    }
    if (!isRecord(parsed)) {
        throw new LifesciBridgeError(`lifesci python command returned JSON that is not an envelope object.\n` +
            `--- stdout ---\n${stdoutSnippet(text)}${formatStderr(stderr)}`, { code: "BAD_ENVELOPE" });
    }
    if (typeof parsed.ok !== "boolean") {
        throw new LifesciBridgeError(`lifesci envelope is missing the boolean "ok" field (command: ${String(parsed.command ?? "unknown")}).\n` +
            `--- stdout ---\n${stdoutSnippet(text)}${formatStderr(stderr)}`, { code: "BAD_ENVELOPE" });
    }
    return parsed;
}
/**
 * Render a failed envelope as the agent-facing error message.
 *
 * The leading `[CODE] message` is the stable contract; `retriable` and
 * `fallbacks` are appended when the Python layer reports them so the agent can
 * decide between retrying, switching source, or reporting the limitation.
 *
 * @param envelope - an envelope with `ok:false`.
 * @returns A multi-line error message.
 */
export function formatEnvelopeError(envelope) {
    const error = isRecord(envelope.error) ? envelope.error : {};
    const code = typeof error.code === "string" && error.code.length > 0 ? error.code : "UNKNOWN";
    const message = typeof error.message === "string" && error.message.length > 0
        ? error.message
        : "lifesci command failed without an error message";
    const lines = [`[${code}] ${message}`];
    if (typeof error.retriable === "boolean")
        lines.push(`retriable: ${error.retriable}`);
    const fallbacks = formatFallbacks(error.fallbacks);
    if (fallbacks !== undefined)
        lines.push(`fallbacks: ${fallbacks}`);
    return lines.join("\n");
}
function formatFallbacks(fallbacks) {
    if (fallbacks === undefined || fallbacks === null)
        return undefined;
    if (Array.isArray(fallbacks)) {
        const entries = fallbacks.filter((entry) => typeof entry === "string");
        if (entries.length === fallbacks.length && entries.length > 0)
            return entries.join(", ");
        if (fallbacks.length === 0)
            return undefined;
        return JSON.stringify(fallbacks);
    }
    if (typeof fallbacks === "string" && fallbacks.length > 0)
        return fallbacks;
    return JSON.stringify(fallbacks);
}
/** Actionable ENOENT message for a missing Python interpreter. */
export function missingPythonMessage(invocation) {
    return (`python interpreter not found: "${invocation.command}". ` +
        `Set the lifesci plugin config "pythonPath" or the LIFESCI_PYTHON environment variable to a ` +
        `Python >= 3.11 interpreter that can import the lifesci package (a source checkout uses ` +
        `<repo>/.venv/bin/python). The bridge sets PYTHONPATH to "${invocation.pythonPackageDir}".`);
}
/**
 * Spawn one child process, enforce the timeout and cancellation, and buffer its
 * output up to {@link MAX_BUFFER_BYTES}.
 *
 * A non-zero exit is a normal result (the JSON envelope decides success); only
 * spawn failures, timeouts, aborts, and buffer overflows reject.
 *
 * @returns Resolves with captured output once the child closes.
 */
export function runProcess(command, args, options) {
    const maxBufferBytes = options.maxBufferBytes ?? MAX_BUFFER_BYTES;
    return new Promise((resolveResult, rejectResult) => {
        if (options.signal?.aborted) {
            rejectResult(new LifesciBridgeError(`${options.label} aborted before start`, { code: "ABORTED" }));
            return;
        }
        if (!existsSync(options.cwd)) {
            rejectResult(new LifesciBridgeError(`${options.label}: working directory does not exist: ${options.cwd}. ` +
                "The session workspace may have been deleted or moved.", { code: "BAD_CWD" }));
            return;
        }
        const stdoutChunks = [];
        const stderrChunks = [];
        let stdoutBytes = 0;
        let stderrBytes = 0;
        let settled = false;
        let timedOut = false;
        let aborted = false;
        let timer;
        let killTimer;
        let child;
        const cleanup = () => {
            if (timer !== undefined)
                clearTimeout(timer);
            if (killTimer !== undefined)
                clearTimeout(killTimer);
            options.signal?.removeEventListener("abort", onAbort);
        };
        const fail = (error) => {
            if (settled)
                return;
            settled = true;
            cleanup();
            rejectResult(error);
        };
        const signalTree = (signal) => {
            const pid = child.pid;
            if (pid !== undefined && process.platform !== "win32") {
                try {
                    // Negative pid = the process group created by `detached: true`.
                    process.kill(-pid, signal);
                }
                catch {
                    // Fall back to the direct child below (group may not exist yet).
                }
            }
            try {
                child.kill(signal);
            }
            catch {
                // The child may already be gone; the close handler still settles the promise.
            }
        };
        const terminate = () => {
            if (child.exitCode !== null || child.signalCode !== null)
                return;
            signalTree("SIGTERM");
            killTimer = setTimeout(() => {
                if (child.exitCode === null && child.signalCode === null)
                    signalTree("SIGKILL");
            }, KILL_GRACE_MS);
            killTimer.unref?.();
        };
        const onAbort = () => {
            aborted = true;
            terminate();
        };
        try {
            child = spawn(command, [...args], {
                cwd: options.cwd,
                env: options.env,
                stdio: ["ignore", "pipe", "pipe"],
                windowsHide: true,
                // POSIX: start a new process group so a timeout/abort can signal the whole
                // tree (Foldseek/PyMOL/ssh descendants), not only the Python parent.
                detached: process.platform !== "win32",
            });
        }
        catch (error) {
            fail(new LifesciBridgeError(`${options.label} could not start: ${errorMessage(error)}`, { code: "SPAWN_FAILED", cause: error }));
            return;
        }
        options.signal?.addEventListener("abort", onAbort, { once: true });
        // Close the race between the pre-spawn check and listener registration: an
        // already-aborted signal never fires a new "abort" event.
        if (options.signal?.aborted)
            onAbort();
        timer = setTimeout(() => {
            timedOut = true;
            terminate();
        }, options.timeoutMs);
        timer.unref?.();
        child.stdout?.on("data", (chunk) => {
            stdoutBytes += chunk.length;
            if (stdoutBytes > maxBufferBytes) {
                fail(new LifesciBridgeError(`${options.label}: stdout exceeded ${maxBufferBytes} bytes; the Python process was killed.`, { code: "OUTPUT_TOO_LARGE" }));
                terminate();
                return;
            }
            stdoutChunks.push(chunk);
        });
        child.stderr?.on("data", (chunk) => {
            // Stderr is diagnostics only; cap it so a runaway traceback cannot exhaust memory.
            stderrBytes += chunk.length;
            if (stderrBytes > maxBufferBytes) {
                stderrChunks.push(Buffer.from("\n[lifesci bridge: stderr truncated]\n"));
                return;
            }
            stderrChunks.push(chunk);
        });
        child.on("error", (error) => {
            if (error.code === "ENOENT") {
                fail(new LifesciBridgeError(options.missingCommandMessage ?? `${options.label}: executable not found: ${command}`, { code: "ENOENT", cause: error }));
                return;
            }
            fail(new LifesciBridgeError(`${options.label} failed to start: ${errorMessage(error)}`, {
                code: "SPAWN_FAILED",
                cause: error,
            }));
        });
        child.on("close", (exitCode, signal) => {
            if (settled)
                return;
            settled = true;
            cleanup();
            const stdout = Buffer.concat(stdoutChunks).toString("utf8");
            const stderr = Buffer.concat(stderrChunks).toString("utf8");
            if (timedOut) {
                rejectResult(new LifesciBridgeError(`${options.label} timed out after ${options.timeoutMs} ms; the Python process was killed.` +
                    formatStderr(stderr), { code: "TIMEOUT" }));
                return;
            }
            if (aborted || options.signal?.aborted) {
                rejectResult(new LifesciBridgeError(`${options.label} was aborted; the Python process was killed.`, {
                    code: "ABORTED",
                }));
                return;
            }
            resolveResult({ stdout, stderr, exitCode, signal });
        });
    });
}
/** Provenance keys kept in the compact agent-facing diagnostics. */
const COMPACT_PROVENANCE_KEYS = [
    "kind",
    "time",
    "method",
    "url",
    "final_url",
    "status",
    "sha256",
    "bytes",
    "cache",
    "database",
    "release",
    "retries",
    "elapsed_ms",
    "path",
    "dataset",
    "name",
    "exit_code",
    "command",
];
/**
 * Build the model-facing diagnostics block for one command envelope.
 *
 * The Python envelope carries mandatory scientific warnings, human-review flags
 * and the provenance trail; the bridge must not silently drop them.
 * Provenance is compacted (allow-listed keys, bounded entry count) so it informs
 * the model without flooding the context; the full log remains on disk.
 */
export function compactDiagnostics(envelope) {
    const provenance = Array.isArray(envelope.provenance) ? envelope.provenance : [];
    const compact = [];
    for (const entry of provenance.slice(0, 40)) {
        if (!isRecord(entry))
            continue;
        const row = {};
        for (const key of COMPACT_PROVENANCE_KEYS) {
            if (entry[key] !== undefined)
                row[key] = entry[key];
        }
        compact.push(row);
    }
    return {
        command: envelope.command,
        version: envelope.version,
        offline: envelope.offline === true,
        dry_run: envelope.dry_run === true,
        cache_dir: envelope.cache_dir,
        warnings: Array.isArray(envelope.warnings) ? envelope.warnings : [],
        human_review: Array.isArray(envelope.human_review) ? envelope.human_review : [],
        provenance: compact,
        provenance_entries: provenance.length,
        provenance_truncated: provenance.length > compact.length,
    };
}
/**
 * Merge the envelope's diagnostics into the tool result the model receives.
 *
 * Object payloads keep their top-level fields (so existing callers/tests can read
 * `payload.n` etc.) and gain a `_lifesci` block; non-object payloads are wrapped
 * as `{ value, _lifesci }`.
 */
export function withEnvelopeDiagnostics(envelope) {
    const diagnostics = compactDiagnostics(envelope);
    const data = envelope.data;
    if (isRecord(data))
        return { ...data, _lifesci: diagnostics };
    return { value: data === undefined ? {} : data, _lifesci: diagnostics };
}
/** Session workspace recorded in the agent's session header, if any. */
export function sessionWorkingDirectory(exec) {
    const cwd = exec?.agent?.session?.header?.cwd;
    return typeof cwd === "string" && cwd.trim().length > 0 ? cwd : undefined;
}
/**
 * Run one command through the Python CLI and return the envelope's `data`.
 *
 * @param spec - generated tool spec (command name and timeout).
 * @param args - model-supplied arguments.
 * @param config - resolved plugin configuration.
 * @param exec - harness execution context (cancellation signal and session cwd).
 * @param options - injectable environment/filesystem facts (tests).
 * @returns The command's `data` payload.
 * @throws {LifesciBridgeError} on spawn failure, timeout, abort, or `ok:false`.
 */
export async function runTool(spec, args, config, exec, options = {}) {
    if (exec?.signal?.aborted) {
        throw new LifesciBridgeError(`lifesci ${spec.command} aborted before start`, { code: "ABORTED" });
    }
    const cwd = options.cwd ?? sessionWorkingDirectory(exec) ?? process.cwd();
    const invocation = buildInvocation({
        spec,
        args,
        config,
        cwd,
        env: options.env,
        repoRoot: options.repoRoot,
        platform: options.platform,
        exists: options.exists,
    });
    const label = `lifesci ${spec.command}`;
    const result = await runProcess(invocation.command, invocation.args, {
        cwd: invocation.cwd,
        env: invocation.env,
        timeoutMs: invocation.timeoutMs,
        signal: exec?.signal,
        label,
        missingCommandMessage: missingPythonMessage(invocation),
    });
    let envelope;
    try {
        envelope = parseEnvelope(result.stdout, result.stderr);
    }
    catch (error) {
        const exitSuffix = result.exitCode !== null && result.exitCode !== 0 ? ` (process exit code ${result.exitCode})` : "";
        if (error instanceof LifesciBridgeError) {
            throw new LifesciBridgeError(`${error.message}${exitSuffix}`, { code: error.code, cause: error });
        }
        throw error;
    }
    if (!envelope.ok)
        throw new LifesciBridgeError(formatEnvelopeError(envelope), { code: envelope.error?.code });
    return withEnvelopeDiagnostics(envelope);
}
