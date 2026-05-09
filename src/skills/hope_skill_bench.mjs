// hope_skill_bench.mjs — benchmarker for Hope skills.
//
// Wraps a skill invocation with timing + success capture. Writes one telemetry
// record to AgentDB `hope-skills-telemetry` AND mirrors to a local JSONL
// (logs/hope-skill-telemetry.jsonl) so read-side aggregation (stats/report,
// evolve's p95/success checks) is reliable.
//
// "Invocation" = spawn `claude -p` with a prompt of the form
//   "Invoke skill /<slug> against this input: <test_input>"
// and consider it a success if the CLI exited 0 and produced non-empty output.
//
// Usage (CLI):
//   hope_skill_bench.mjs <skill> [--input "<text>"] [--runs N]
// Default input: the skill's own SKILL.md description field.

import { promises as fs } from "node:fs";
import path from "node:path";
import {
  NS_TELEMETRY,
  memStore,
  telemetryWrite,
  runClaude,
  skillDirFor,
  nowIso,
} from "./hope_skill_common.mjs";

async function readSkill(nameOrSlug) {
  const dir = await skillDirFor(nameOrSlug);
  if (!dir) throw new Error(`skill not found: ${nameOrSlug}`);
  const md = await fs.readFile(path.join(dir, "SKILL.md"), "utf8");
  const descMatch = md.match(/description:\s*([^\n]+)/i);
  const nameMatch = md.match(/^name:\s*([^\n]+)/im);
  return {
    dir,
    body: md,
    name: (nameMatch?.[1] || nameOrSlug).trim(),
    description: (descMatch?.[1] || "").trim(),
  };
}

function invokePrompt(name, body, input) {
  return `Invoke the skill below against this input. Produce the output the skill would yield.

--- SKILL (${name}) ---
${body}
--- END SKILL ---

INPUT:
${input}`;
}

export async function benchOnce(nameOrSlug, input, { bodyOverride, nameOverride, turnId } = {}) {
  const skill = await readSkill(nameOrSlug);
  const body = bodyOverride ?? skill.body;
  const name = nameOverride ?? skill.name;
  const resolvedInput = input || skill.description || nameOrSlug;

  const start = Date.now();
  let success = false;
  let output = "";
  let errMsg = null;
  try {
    output = await runClaude(invokePrompt(name, body, resolvedInput), { timeoutMs: 180_000 });
    success = Boolean(output && output.trim().length > 0);
  } catch (e) {
    errMsg = String(e?.message || e);
    success = false;
  }
  const elapsed_ms = Date.now() - start;

  const record = {
    skill: nameOrSlug,
    invoked_at: nowIso(),
    elapsed_ms,
    success,
    turn_id: turnId ?? `bench-${Date.now()}`,
    input_len: resolvedInput.length,
    output_len: output.length,
    error: errMsg,
  };

  // Best-effort AgentDB write + local mirror (mirror is authoritative for reads).
  try {
    await memStore(NS_TELEMETRY, `tele:${nameOrSlug}:${record.turn_id}`, record);
  } catch (e) {
    record._agentdb_err = String(e?.message || e);
  }
  await telemetryWrite(record);
  return record;
}

export async function benchMany(nameOrSlug, input, runs = 1, opts = {}) {
  const results = [];
  for (let i = 0; i < runs; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    const r = await benchOnce(nameOrSlug, input, {
      ...opts,
      turnId: `bench-${Date.now()}-${i}`,
    });
    results.push(r);
  }
  return results;
}

// --- CLI entry ---------------------------------------------------------------
if (import.meta.url === `file://${process.argv[1]}`) {
  const argv = process.argv.slice(2);
  let skill = null;
  let input = null;
  let runs = 1;
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--input") input = argv[++i];
    else if (a === "--runs") runs = Number.parseInt(argv[++i], 10) || 1;
    else if (!skill) skill = a;
  }
  if (!skill) {
    process.stderr.write("usage: hope_skill_bench.mjs <skill> [--input \"text\"] [--runs N]\n");
    process.exit(2);
  }
  benchMany(skill, input, runs)
    .then((rows) => {
      process.stdout.write(JSON.stringify({ skill, runs: rows.length, results: rows }, null, 2) + "\n");
      process.exit(0);
    })
    .catch((e) => {
      process.stdout.write(JSON.stringify({ status: "error", error: String(e?.message || e) }) + "\n");
      process.exit(1);
    });
}
