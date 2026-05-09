#!/usr/bin/env node
/**
 * hope_iterate.mjs — autonomous test → fix → commit loop for Hope.
 *
 * Watches a project directory, runs its test command on every debounced change,
 * and if tests fail spawns a `claude -p` fixer subprocess (inherits Joel's Max
 * auth). When tests go green, stages the changed files and either commits
 * automatically (guarded) or prints a diff for the user to confirm.
 *
 * Public API:
 *   runOnce({ cwd, config })                 → runs one full cycle, returns report
 *   startWatch({ cwd, config, onCycle })     → fs.watch based loop, returns stopper
 *   loadConfig(cwd)                          → merges .hope-iterate.json + autodetect
 *   buildFixerPrompt({ testOut, changed })   → exposed for skill / CLI use
 *
 * Safety (all enforced in code, not just docs):
 *   - max_iterations hard-cap per trigger (default 3)
 *   - per-cycle test timeout (default 5 min, configurable)
 *   - exponential backoff between fixer cycles
 *   - never auto-commit to main/master unless auto_commit AND allow_main both true
 *   - never force-push; we only call `git add` + `git commit`
 *   - on give-up, `git stash push` the fixer's mutations so the tree stays clean
 *   - every cycle appended to ~/Documents/Github/Hope/.hope-io/iterate.jsonl
 */

import { spawn, spawnSync } from "node:child_process";
import { createWriteStream } from "node:fs";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

/* -------------------------------------------------------------------------- */
/*  Constants & paths                                                          */
/* -------------------------------------------------------------------------- */

const HOPE_ROOT = "/Users/joelc/Documents/Github/Hope";
const LOG_PATH = path.join(HOPE_ROOT, ".hope-io", "iterate.jsonl");

const DEFAULTS = Object.freeze({
  test_cmd: null, // auto-detect
  watch_globs: ["**/*"],
  auto_commit: false,
  allow_main: false,
  max_iterations: 3,
  test_timeout_ms: 5 * 60 * 1000,
  debounce_ms: 2000,
  backoff_base_ms: 1500,
  commit_message_template:
    "hope-iterate: green after {iterations} cycle(s)\n\n{summary}",
  claude_bin: "/Users/joelc/.local/bin/claude",
});

/* -------------------------------------------------------------------------- */
/*  Logging                                                                    */
/* -------------------------------------------------------------------------- */

async function appendLog(entry) {
  try {
    await fs.mkdir(path.dirname(LOG_PATH), { recursive: true });
    const line = JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n";
    await fs.appendFile(LOG_PATH, line, "utf8");
  } catch {
    /* logging must never crash the loop */
  }
}

/* -------------------------------------------------------------------------- */
/*  Config loading & test-runner autodetect                                    */
/* -------------------------------------------------------------------------- */

async function fileExists(p) {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

async function autodetectTestCmd(cwd) {
  const pkgPath = path.join(cwd, "package.json");
  if (await fileExists(pkgPath)) {
    try {
      const pkg = JSON.parse(await fs.readFile(pkgPath, "utf8"));
      if (pkg?.scripts?.test) return "npm test";
    } catch {
      /* fall through */
    }
  }
  if (
    (await fileExists(path.join(cwd, "pyproject.toml"))) ||
    (await fileExists(path.join(cwd, "pytest.ini")))
  ) {
    return "pytest";
  }
  if (await fileExists(path.join(cwd, "Cargo.toml"))) return "cargo test";
  if (await fileExists(path.join(cwd, "Makefile"))) {
    try {
      const mk = await fs.readFile(path.join(cwd, "Makefile"), "utf8");
      if (/^test:/m.test(mk)) return "make test";
    } catch {
      /* ignore */
    }
  }
  return null;
}

export async function loadConfig(cwd) {
  const file = path.join(cwd, ".hope-iterate.json");
  let user = {};
  if (await fileExists(file)) {
    try {
      user = JSON.parse(await fs.readFile(file, "utf8"));
    } catch (err) {
      throw new Error(`Invalid .hope-iterate.json: ${err.message}`);
    }
  }
  const merged = { ...DEFAULTS, ...user };
  if (!merged.test_cmd) {
    merged.test_cmd = await autodetectTestCmd(cwd);
  }
  if (!merged.test_cmd) {
    throw new Error(
      "No test command found. Add .hope-iterate.json with {test_cmd: \"...\"} " +
        "or add one of: package.json#scripts.test, pytest.ini, pyproject.toml, " +
        "Cargo.toml, Makefile with test target."
    );
  }
  return merged;
}

/* -------------------------------------------------------------------------- */
/*  Git helpers                                                                */
/* -------------------------------------------------------------------------- */

function git(cwd, args, opts = {}) {
  const res = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    ...opts,
  });
  return {
    ok: res.status === 0,
    stdout: (res.stdout || "").trim(),
    stderr: (res.stderr || "").trim(),
    status: res.status,
  };
}

function currentBranch(cwd) {
  const r = git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"]);
  return r.ok ? r.stdout : null;
}

function changedFiles(cwd) {
  // Tracked + untracked, relative paths.
  // Porcelain v1 is: "XY path" where X/Y are 1 char each and then a space.
  // NOTE: git() trims leading whitespace from stdout as a whole, so the
  // first line can be missing its leading " " in XY. We use -z + raw
  // stdout to avoid the trim path entirely.
  const res = spawnSync("git", ["status", "--porcelain=v1", "-z"], {
    cwd,
    encoding: "utf8",
  });
  if (res.status !== 0) return [];
  const ignored = new Set([
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    ".hope-io",
    ".DS_Store",
  ]);
  const out = [];
  // With -z, entries are NUL-separated. Renames are two NUL-separated tokens:
  // "R  new" + NUL + "old". We only keep the new path.
  const tokens = (res.stdout || "").split("\u0000");
  let skipNext = false;
  for (const raw of tokens) {
    if (skipNext) { skipNext = false; continue; }
    if (!raw) continue;
    // XY<space>path — prefix is exactly 3 chars in -z mode (no trim).
    const xy = raw.slice(0, 2);
    const p = raw.slice(3);
    if (xy[0] === "R" || xy[0] === "C") skipNext = true;
    const clean = p;
    if (!clean) continue;
    if (clean.endsWith("/")) continue;
    if (clean.split("/").some((seg) => ignored.has(seg))) continue;
    out.push(clean);
  }
  return out;
}

function lastGreenRef(cwd) {
  // A tag/commit we mark when tests pass. Falls back to HEAD~1 or HEAD.
  const tagged = git(cwd, ["rev-parse", "hope-iterate/last-green"]);
  if (tagged.ok) return tagged.stdout;
  const parent = git(cwd, ["rev-parse", "HEAD~1"]);
  if (parent.ok) return parent.stdout;
  return git(cwd, ["rev-parse", "HEAD"]).stdout || null;
}

function markLastGreen(cwd) {
  git(cwd, ["tag", "-f", "hope-iterate/last-green", "HEAD"]);
}

function filesChangedSince(cwd, ref) {
  if (!ref) return changedFiles(cwd);
  const r = git(cwd, ["diff", "--name-only", ref, "--"]);
  if (!r.ok) return changedFiles(cwd);
  return r.stdout.split("\n").filter(Boolean).concat(changedFiles(cwd));
}

function gitStash(cwd, label) {
  return git(cwd, ["stash", "push", "-u", "-m", label]);
}

function isProtectedBranch(branch) {
  return branch === "main" || branch === "master";
}

/* -------------------------------------------------------------------------- */
/*  Test runner                                                                */
/* -------------------------------------------------------------------------- */

function runTest(cwd, testCmd, timeoutMs) {
  return new Promise((resolve) => {
    const started = Date.now();
    const proc = spawn(testCmd, {
      cwd,
      shell: true,
      env: { ...process.env, CI: "1", FORCE_COLOR: "0" },
    });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      proc.kill("SIGKILL");
    }, timeoutMs);

    proc.stdout.on("data", (c) => (stdout += c.toString()));
    proc.stderr.on("data", (c) => (stderr += c.toString()));
    proc.on("close", (code, signal) => {
      clearTimeout(timer);
      resolve({
        exit_code: code,
        signal,
        timed_out: timedOut,
        stdout,
        stderr,
        elapsed_ms: Date.now() - started,
        passed: code === 0 && !timedOut,
      });
    });
    proc.on("error", (err) => {
      clearTimeout(timer);
      resolve({
        exit_code: -1,
        error: err.message,
        stdout,
        stderr,
        elapsed_ms: Date.now() - started,
        passed: false,
      });
    });
  });
}

/* -------------------------------------------------------------------------- */
/*  Fixer — claude CLI subprocess (Max auth)                                   */
/* -------------------------------------------------------------------------- */

export function buildFixerPrompt({ testOut, changed, iteration, maxIterations }) {
  const tail = (s, n = 4000) => (s && s.length > n ? "…\n" + s.slice(-n) : s || "");
  return [
    "You are Hope's autonomous test-fixer. The user's test suite is failing.",
    `This is fixer iteration ${iteration}/${maxIterations}.`,
    "",
    "RULES:",
    "- Fix the failure with the minimum code change possible.",
    "- Edit only source files; do NOT modify test files unless the test itself is clearly wrong.",
    "- Do not add new dependencies, do not refactor unrelated code.",
    "- Do not touch .hope-iterate.json, .hope-io/, .claude/, or git config.",
    "- When you finish, leave the tree ready for the test runner to pick up.",
    "",
    "FAILING TEST OUTPUT (tail):",
    "```",
    tail(testOut.stdout, 3000),
    "--- stderr ---",
    tail(testOut.stderr, 2000),
    "```",
    "",
    "FILES CHANGED SINCE LAST GREEN:",
    changed.length ? changed.map((f) => `- ${f}`).join("\n") : "(none tracked)",
    "",
    "Now fix the failure. Do not ask for confirmation.",
  ].join("\n");
}

function runFixer({ claudeBin, cwd, prompt, timeoutMs }) {
  return new Promise((resolve) => {
    const started = Date.now();
    // `-p` = print (non-interactive). Inherits the user's Max auth.
    const proc = spawn(
      claudeBin,
      ["-p", "--dangerously-skip-permissions", prompt],
      { cwd, env: process.env }
    );
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      proc.kill("SIGKILL");
    }, timeoutMs);
    proc.stdout.on("data", (c) => (stdout += c.toString()));
    proc.stderr.on("data", (c) => (stderr += c.toString()));
    proc.on("close", (code) => {
      clearTimeout(timer);
      resolve({
        exit_code: code,
        timed_out: timedOut,
        stdout,
        stderr,
        elapsed_ms: Date.now() - started,
      });
    });
    proc.on("error", (err) =>
      resolve({ exit_code: -1, error: err.message, stdout, stderr, elapsed_ms: Date.now() - started })
    );
  });
}

/* -------------------------------------------------------------------------- */
/*  Commit helpers                                                             */
/* -------------------------------------------------------------------------- */

function renderCommitMsg(template, ctx) {
  return template
    .replace(/\{iterations\}/g, String(ctx.iterations))
    .replace(/\{summary\}/g, ctx.summary || "");
}

function stageChanged(cwd, files) {
  if (!files.length) return { ok: true, stdout: "" };
  return git(cwd, ["add", "--", ...files]);
}

function diffStaged(cwd) {
  return git(cwd, ["diff", "--cached"]).stdout;
}

function commitStaged(cwd, msg) {
  return git(cwd, ["commit", "-m", msg]);
}

/* -------------------------------------------------------------------------- */
/*  Core: one full trigger cycle                                               */
/* -------------------------------------------------------------------------- */

export async function runOnce({ cwd, config: userConfig, reason = "manual" } = {}) {
  const cfg = userConfig ? { ...DEFAULTS, ...userConfig } : await loadConfig(cwd);
  if (!cfg.test_cmd) throw new Error("No test_cmd after config load");
  const branch = currentBranch(cwd);
  const refBefore = lastGreenRef(cwd);
  const cycleId = `iter-${Date.now()}`;
  await appendLog({ cycleId, phase: "start", reason, cwd, branch, cfg: redact(cfg) });

  let iteration = 0;
  let lastTest = null;
  let lastFixer = null;

  while (iteration <= cfg.max_iterations) {
    lastTest = await runTest(cwd, cfg.test_cmd, cfg.test_timeout_ms);
    await appendLog({
      cycleId,
      phase: "test",
      iteration,
      passed: lastTest.passed,
      exit_code: lastTest.exit_code,
      timed_out: lastTest.timed_out,
      elapsed_ms: lastTest.elapsed_ms,
      stdout_tail: tail(lastTest.stdout, 1500),
      stderr_tail: tail(lastTest.stderr, 1500),
    });

    if (lastTest.passed) {
      // GREEN path
      const changed = changedFiles(cwd);
      const stage = stageChanged(cwd, changed);
      const msg = renderCommitMsg(cfg.commit_message_template, {
        iterations: iteration,
        summary:
          `Test: ${cfg.test_cmd} ✓\n` +
          `Files: ${changed.length}\n` +
          `Cycle: ${cycleId}`,
      });

      const protectedBranch = isProtectedBranch(branch);
      const canAutoCommit =
        cfg.auto_commit && (!protectedBranch || cfg.allow_main);

      let commitRes = null;
      if (canAutoCommit && stage.ok && changed.length) {
        commitRes = commitStaged(cwd, msg);
        if (commitRes.ok) markLastGreen(cwd);
      }

      await appendLog({
        cycleId,
        phase: "green",
        iteration,
        branch,
        protectedBranch,
        auto_commit: cfg.auto_commit,
        allow_main: cfg.allow_main,
        committed: !!(commitRes && commitRes.ok),
        changed,
      });

      return {
        ok: true,
        green: true,
        iterations: iteration,
        branch,
        changed,
        commit_message: msg,
        committed: !!(commitRes && commitRes.ok),
        commit_blocked_reason:
          canAutoCommit || !changed.length
            ? null
            : protectedBranch && !cfg.allow_main
            ? "protected-branch"
            : "auto_commit=false",
        diff: canAutoCommit ? null : diffStaged(cwd),
        test: summarizeTest(lastTest),
      };
    }

    // RED path: bail if we're at the cap
    if (iteration >= cfg.max_iterations) break;

    // Backoff then fix
    const backoff = cfg.backoff_base_ms * Math.pow(2, iteration);
    await sleep(backoff);

    const changedSince = filesChangedSince(cwd, refBefore);
    const prompt = buildFixerPrompt({
      testOut: lastTest,
      changed: changedSince,
      iteration: iteration + 1,
      maxIterations: cfg.max_iterations,
    });
    lastFixer = await runFixer({
      claudeBin: cfg.claude_bin,
      cwd,
      prompt,
      timeoutMs: cfg.test_timeout_ms, // fixer shares the same ceiling
    });
    await appendLog({
      cycleId,
      phase: "fixer",
      iteration: iteration + 1,
      exit_code: lastFixer.exit_code,
      timed_out: lastFixer.timed_out,
      elapsed_ms: lastFixer.elapsed_ms,
      prompt,
      fixer_stdout_tail: tail(lastFixer.stdout, 2000),
      fixer_stderr_tail: tail(lastFixer.stderr, 1500),
    });

    iteration += 1;
  }

  // Give up — stash whatever the fixer did so the tree is clean.
  const stash = gitStash(cwd, `hope-iterate-failed-${cycleId}`);
  await appendLog({
    cycleId,
    phase: "bail",
    iteration,
    stashed: stash.ok,
    stash_stderr: stash.stderr,
  });

  return {
    ok: false,
    green: false,
    iterations: iteration,
    branch,
    bailed: true,
    stashed: stash.ok,
    last_test: summarizeTest(lastTest),
    last_fixer: lastFixer
      ? {
          exit_code: lastFixer.exit_code,
          timed_out: lastFixer.timed_out,
          elapsed_ms: lastFixer.elapsed_ms,
        }
      : null,
    summary:
      `hope-iterate gave up after ${iteration} cycles on ${branch}. ` +
      `Fixer changes stashed as hope-iterate-failed-${cycleId}. ` +
      `Review ${LOG_PATH} for the full trace.`,
  };
}

/* -------------------------------------------------------------------------- */
/*  Watch mode                                                                 */
/* -------------------------------------------------------------------------- */

const IGNORE_DIR = new Set([
  ".git",
  "node_modules",
  ".venv",
  "venv",
  "__pycache__",
  ".pytest_cache",
  ".ruff_cache",
  "target",
  "dist",
  "build",
  ".hope-io",
  ".claude-flow",
  ".next",
]);

export async function startWatch({ cwd, config: userCfg, onCycle } = {}) {
  const cfg = userCfg ? { ...DEFAULTS, ...userCfg } : await loadConfig(cwd);
  const watchers = [];
  let debounceTimer = null;
  let running = false;
  let queued = false;

  const trigger = async (reason) => {
    if (running) {
      queued = true;
      return;
    }
    running = true;
    try {
      const report = await runOnce({ cwd, config: cfg, reason });
      if (onCycle) onCycle(report);
    } finally {
      running = false;
      if (queued) {
        queued = false;
        trigger("queued");
      }
    }
  };

  const onChange = (filename) => {
    if (!filename) return;
    const parts = filename.split(path.sep);
    if (parts.some((p) => IGNORE_DIR.has(p))) return;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => trigger(`change:${filename}`), cfg.debounce_ms);
  };

  // fs.watch with recursive is macOS/Windows only, which is fine for Hope.
  const { watch } = await import("node:fs");
  const w = watch(cwd, { recursive: true, persistent: true }, (_evt, fn) =>
    onChange(fn)
  );
  watchers.push(w);

  // Kick an initial run so the first state gets established.
  trigger("initial");

  return {
    stop: () => {
      clearTimeout(debounceTimer);
      for (const w of watchers) {
        try {
          w.close();
        } catch {
          /* ignore */
        }
      }
    },
    cfg,
  };
}

/* -------------------------------------------------------------------------- */
/*  Utilities                                                                  */
/* -------------------------------------------------------------------------- */

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function tail(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(-n) : s;
}

function redact(cfg) {
  const { claude_bin, ...rest } = cfg;
  return { ...rest, claude_bin: claude_bin ? "[set]" : null };
}

function summarizeTest(t) {
  if (!t) return null;
  return {
    passed: t.passed,
    exit_code: t.exit_code,
    timed_out: t.timed_out,
    elapsed_ms: t.elapsed_ms,
    stdout_tail: tail(t.stdout, 800),
    stderr_tail: tail(t.stderr, 800),
  };
}

export { DEFAULTS, LOG_PATH };
