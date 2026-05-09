// hope_skill_common.mjs — shared helpers for Hope's self-evolving skill system.
//
// Responsibilities:
//   - slug()          : safe filesystem slug from a natural-language description.
//   - runClaude()     : spawn the `claude -p` CLI (Max auth, no SDK keys), return stdout.
//   - memStore/Search/Retrieve : thin wrappers around `claude-flow memory …` for
//                               writing to AgentDB namespaces.
//   - telemetryWrite()/Read()  : append-only JSONL mirror at logs/hope-skill-telemetry.jsonl
//                               so read-side aggregation does not depend on the
//                               list subcommand (which is unreliable) of the CLI.
//   - skillPath()     : resolve generated-skill directory (canonical, not symlink).
//
// No external deps — pure Node stdlib.

import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import path from "node:path";
import os from "node:os";

export const HOPE_ROOT = "/Users/joelc/Documents/Github/Hope";
export const GENERATED_DIR = path.join(HOPE_ROOT, "src/skills/generated");
export const SKILLS_LINK_DIR = path.join(os.homedir(), ".claude/skills");
export const LOG_DIR = path.join(HOPE_ROOT, "logs");
export const TELEMETRY_JSONL = path.join(LOG_DIR, "hope-skill-telemetry.jsonl");
export const EVOLUTION_JSONL = path.join(LOG_DIR, "hope-skill-evolution.jsonl");

export const NS_INDEX = "skills-index";
export const NS_EVAL = "hope-skills-eval";
export const NS_TELEMETRY = "hope-skills-telemetry";
export const NS_EVOLUTION = "hope-skills-evolution";

export function slug(desc) {
  return String(desc)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}

export function nowIso() {
  return new Date().toISOString();
}

// Run a command and capture stdout/stderr + exit. Never throws on non-zero;
// callers inspect `code`.
export function runCmd(cmd, args, { input, cwd, timeoutMs = 120_000, env } = {}) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, {
      cwd,
      env: env ?? process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try { child.kill("SIGKILL"); } catch {}
    }, timeoutMs);
    child.stdout.on("data", (b) => (stdout += b.toString("utf8")));
    child.stderr.on("data", (b) => (stderr += b.toString("utf8")));
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? 1, stdout, stderr, timedOut });
    });
    if (input !== undefined) {
      child.stdin.write(input);
    }
    child.stdin.end();
  });
}

// Spawn `claude -p <prompt>` — Max-subscription auth (OAuth via keychain).
// Do NOT pass --bare: it strips OAuth and forces API-key auth, which breaks
// Max-subscription cognition. We accept the slight stdout noise from hooks.
export async function runClaude(prompt, { timeoutMs = 180_000, model } = {}) {
  const args = ["-p"];
  if (model) {
    args.push("--model", model);
  }
  args.push(prompt);
  const { code, stdout, stderr, timedOut } = await runCmd("claude", args, {
    timeoutMs,
  });
  if (code !== 0) {
    throw new Error(
      `claude -p failed (code=${code}, timedOut=${timedOut}): ${stderr.slice(0, 500)}`,
    );
  }
  return stdout.trim();
}

// --- AgentDB wrappers via claude-flow memory ---------------------------------
export async function memStore(namespace, key, value) {
  const payload = typeof value === "string" ? value : JSON.stringify(value);
  const { code, stderr } = await runCmd(
    "claude-flow",
    ["memory", "store", "-k", key, "-v", payload, "-n", namespace],
    { timeoutMs: 30_000 },
  );
  if (code !== 0) {
    throw new Error(`memStore failed: ${stderr.slice(0, 300)}`);
  }
}

export async function memRetrieve(namespace, key) {
  const { code, stdout } = await runCmd(
    "claude-flow",
    ["memory", "retrieve", "-k", key, "-n", namespace],
    { timeoutMs: 15_000 },
  );
  if (code !== 0) return null;
  // parse out the "Value:" block from the CLI's boxed output
  const m = stdout.match(/\|\s*Value:\s*\|[\s\S]*?\|\s*([\s\S]*?)\s*\|/);
  if (!m) return null;
  return m[1].trim();
}

// --- Local JSONL mirror for telemetry ----------------------------------------
export async function telemetryWrite(record) {
  await fs.mkdir(LOG_DIR, { recursive: true });
  const line = JSON.stringify({ ts: nowIso(), ...record }) + "\n";
  await fs.appendFile(TELEMETRY_JSONL, line, "utf8");
}

export async function telemetryRead({ skill } = {}) {
  try {
    const raw = await fs.readFile(TELEMETRY_JSONL, "utf8");
    const rows = raw
      .split("\n")
      .filter(Boolean)
      .map((l) => {
        try { return JSON.parse(l); } catch { return null; }
      })
      .filter(Boolean);
    return skill ? rows.filter((r) => r.skill === skill) : rows;
  } catch (err) {
    if (err.code === "ENOENT") return [];
    throw err;
  }
}

export async function evolutionWrite(record) {
  await fs.mkdir(LOG_DIR, { recursive: true });
  const line = JSON.stringify({ ts: nowIso(), ...record }) + "\n";
  await fs.appendFile(EVOLUTION_JSONL, line, "utf8");
}

// Compute p50/p95/success over an array of telemetry rows.
export function summarize(rows) {
  if (!rows.length) {
    return { n: 0, p50: null, p95: null, success_rate: null };
  }
  const elapsed = rows.map((r) => Number(r.elapsed_ms) || 0).sort((a, b) => a - b);
  const pick = (p) => elapsed[Math.min(elapsed.length - 1, Math.floor(p * elapsed.length))];
  const successes = rows.filter((r) => r.success).length;
  return {
    n: rows.length,
    p50: pick(0.5),
    p95: pick(0.95),
    success_rate: successes / rows.length,
  };
}

// Resolve canonical generated-skill dir (preferred) — falls back to the symlink
// target if the caller passed only a slug that's actually a hand-written skill.
export async function skillDirFor(nameOrSlug) {
  const gen = path.join(GENERATED_DIR, nameOrSlug);
  try {
    const st = await fs.stat(gen);
    if (st.isDirectory()) return gen;
  } catch {}
  const link = path.join(SKILLS_LINK_DIR, nameOrSlug);
  try {
    const real = await fs.realpath(link);
    return real;
  } catch {}
  return null;
}

// List generated (Hope-authored) skills only — hand-written skills are never
// touched by evolve without opt-in.
export async function listGeneratedSkills() {
  try {
    const entries = await fs.readdir(GENERATED_DIR, { withFileTypes: true });
    return entries.filter((e) => e.isDirectory()).map((e) => e.name);
  } catch {
    return [];
  }
}
