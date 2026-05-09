#!/usr/bin/env node
/**
 * hope-iterate CLI — thin wrapper around hope_iterate.mjs.
 *
 * Subcommands:
 *   start  <dir>     start a watcher (debounced 2s), pidfile-gated
 *   stop             stop the watcher started by `start`
 *   status           print pidfile status + last log entries
 *   once   <dir>     one-shot: run a single test-fix-commit cycle
 *
 * Pidfile: ~/Documents/Github/Hope/.hope-io/iterate.pid
 * Log:     ~/Documents/Github/Hope/.hope-io/iterate.jsonl
 */

import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";

import { runOnce, startWatch, LOG_PATH } from "./hope_iterate.mjs";

const HOPE_IO = "/Users/joelc/Documents/Github/Hope/.hope-io";
const PID_PATH = path.join(HOPE_IO, "iterate.pid");
const WATCH_LOG = path.join(HOPE_IO, "iterate-watch.log");

function usage() {
  return [
    "hope-iterate — autonomous test → fix → commit loop",
    "",
    "Usage:",
    "  hope-iterate start <dir>   Watch <dir>, run on change, spawn fixer on red",
    "  hope-iterate stop          Stop the background watcher",
    "  hope-iterate status        Show pidfile + last log entries",
    "  hope-iterate once  <dir>   Run one cycle, no watch",
    "",
    "Config: optional .hope-iterate.json in <dir> with:",
    "  { test_cmd, watch_globs, auto_commit, allow_main,",
    "    max_iterations, test_timeout_ms, commit_message_template }",
  ].join("\n");
}

async function readPid() {
  try {
    const txt = await fs.readFile(PID_PATH, "utf8");
    const pid = parseInt(txt.trim(), 10);
    return Number.isFinite(pid) ? pid : null;
  } catch {
    return null;
  }
}

function pidAlive(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function cmdStart(dir) {
  if (!dir) {
    process.stderr.write("hope-iterate start: missing <dir>\n");
    process.exit(2);
  }
  const absDir = path.resolve(dir);
  await fs.mkdir(HOPE_IO, { recursive: true });
  const existing = await readPid();
  if (pidAlive(existing)) {
    process.stderr.write(`hope-iterate already running (pid ${existing})\n`);
    process.exit(1);
  }

  // Re-exec ourselves as a detached child in "watch" mode.
  if (process.env.HOPE_ITERATE_WATCH !== "1") {
    const out = await fs.open(WATCH_LOG, "a");
    const child = spawn(process.execPath, [fileUrlToPath(import.meta.url), "start", absDir], {
      detached: true,
      stdio: ["ignore", out.fd, out.fd],
      env: { ...process.env, HOPE_ITERATE_WATCH: "1" },
    });
    child.unref();
    // The detached child writes its own pid inside its process body.
    await out.close();
    process.stdout.write(
      JSON.stringify({ started: true, pid: child.pid, dir: absDir, log: WATCH_LOG }) + "\n"
    );
    return;
  }

  // Detached child body: actually watch.
  await fs.writeFile(PID_PATH, String(process.pid));
  const { stop } = await startWatch({
    cwd: absDir,
    onCycle: (report) => {
      process.stdout.write(JSON.stringify({ cycle: report }) + "\n");
    },
  });
  const shutdown = async () => {
    stop();
    try { await fs.unlink(PID_PATH); } catch { /* ignore */ }
    process.exit(0);
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}

async function cmdStop() {
  const pid = await readPid();
  if (!pidAlive(pid)) {
    process.stdout.write(JSON.stringify({ stopped: false, reason: "not-running" }) + "\n");
    try { await fs.unlink(PID_PATH); } catch { /* ignore */ }
    return;
  }
  try {
    process.kill(pid, "SIGTERM");
    process.stdout.write(JSON.stringify({ stopped: true, pid }) + "\n");
  } catch (err) {
    process.stdout.write(
      JSON.stringify({ stopped: false, pid, error: err.message }) + "\n"
    );
  }
}

async function cmdStatus() {
  const pid = await readPid();
  const alive = pidAlive(pid);
  let lastLines = [];
  try {
    const raw = await fs.readFile(LOG_PATH, "utf8");
    lastLines = raw.trim().split("\n").slice(-5).map((l) => {
      try { return JSON.parse(l); } catch { return l; }
    });
  } catch { /* no log yet */ }
  process.stdout.write(
    JSON.stringify(
      { running: alive, pid: alive ? pid : null, pidfile: PID_PATH, log: LOG_PATH, last: lastLines },
      null,
      2
    ) + "\n"
  );
}

async function cmdOnce(dir) {
  if (!dir) {
    process.stderr.write("hope-iterate once: missing <dir>\n");
    process.exit(2);
  }
  const absDir = path.resolve(dir);
  const report = await runOnce({ cwd: absDir, reason: "once" });
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
  process.exit(report.ok ? 0 : 1);
}

function fileUrlToPath(u) {
  return new URL(u).pathname;
}

async function main() {
  const [sub, ...rest] = process.argv.slice(2);
  switch (sub) {
    case "start":  return cmdStart(rest[0]);
    case "stop":   return cmdStop();
    case "status": return cmdStatus();
    case "once":   return cmdOnce(rest[0]);
    case "-h":
    case "--help":
    case undefined:
      process.stdout.write(usage() + "\n");
      process.exit(sub ? 0 : 2);
      break;
    default:
      process.stderr.write(`unknown subcommand: ${sub}\n` + usage() + "\n");
      process.exit(2);
  }
}

main().catch((err) => {
  process.stderr.write(`hope-iterate error: ${err.message}\n`);
  process.exit(1);
});
