// hope_skill_evolve.mjs — evolution worker for Hope skills.
//
// Trigger: a skill shows p95 > 3s OR success_rate < 0.8 over >= 20 samples.
// Action:
//   (a) Read <skill_dir>/SKILL.md
//   (b) Ask `claude -p` to rewrite it for clarity+efficiency+robustness, keeping
//       the public contract (name + description) intact.
//   (c) Write candidate to <skill_dir>/SKILL.md.candidate
//   (d) A/B: run 10 invocations with candidate body vs current body, compute
//       p95 + success_rate for both. Promotion rule:
//
//         candidate.success_rate >= current.success_rate
//           AND candidate.p95 <= current.p95
//           AND candidate.success_rate >= 0.8
//
//       Strictly better on BOTH axes (or equal success AND strictly-better p95).
//       On win: back up current to SKILL.md.v<n>.bak, move candidate into place,
//       and log to `hope-skills-evolution`.
//       On loss: discard candidate; log the decision.
//
// Safety gates:
//   - Never evolves skills under ~/.claude/skills that are NOT symlinks into
//     our generated/ dir (protects Joel's hand-written skills).
//   - --auto-promote defaults to false when invoked without the 10-consecutive
//     successful-use trail. Joel opts in either by passing --auto-promote OR by
//     enabling the background runner (src/skills/runner/evolve-loop.sh).

import { promises as fs } from "node:fs";
import path from "node:path";
import {
  GENERATED_DIR,
  NS_EVOLUTION,
  memStore,
  evolutionWrite,
  telemetryRead,
  runClaude,
  skillDirFor,
  summarize,
  nowIso,
  listGeneratedSkills,
} from "./hope_skill_common.mjs";
import { benchOnce } from "./hope_skill_bench.mjs";

const REWRITER_PROMPT = (name, desc, current, telemetrySummary, failures) => `Rewrite the Claude Code Skill below for clarity, efficiency, and robustness.

HARD CONSTRAINTS:
- Do NOT change the public contract: the "name" frontmatter MUST stay "${name}" and the "description" MUST still match the same trigger ("${desc}").
- Keep the skill body focused — cut anything redundant, clarify steps, make invocation deterministic.
- Output ONLY the new SKILL.md (frontmatter + body). No backticks, no commentary.

CURRENT SKILL:
${current}

TELEMETRY SUMMARY (last N samples):
${JSON.stringify(telemetrySummary, null, 2)}

RECENT FAILURE EXAMPLES (if any):
${failures.length ? failures.map((f, i) => `${i + 1}. ${f}`).join("\n") : "(none)"}`;

async function isEvolvable(nameOrSlug) {
  const dir = await skillDirFor(nameOrSlug);
  if (!dir) return { ok: false, reason: "skill not found" };
  if (!dir.startsWith(GENERATED_DIR)) {
    return { ok: false, reason: `refusing to evolve hand-written skill at ${dir}` };
  }
  return { ok: true, dir };
}

function shouldTrigger(stats) {
  if (!stats || stats.n < 20) return { trigger: false, reason: `n=${stats?.n ?? 0} < 20` };
  if (stats.p95 != null && stats.p95 > 3000) return { trigger: true, reason: `p95=${stats.p95}ms > 3000ms` };
  if (stats.success_rate != null && stats.success_rate < 0.8) {
    return { trigger: true, reason: `success_rate=${stats.success_rate.toFixed(2)} < 0.80` };
  }
  return { trigger: false, reason: "within SLO" };
}

function betterThan(candidate, current) {
  if (candidate.success_rate < 0.8) return false;
  if (candidate.success_rate < current.success_rate) return false;
  if (candidate.p95 > current.p95) return false;
  // strict improvement on at least one axis
  return candidate.p95 < current.p95 || candidate.success_rate > current.success_rate;
}

async function nextBackupPath(skillDir) {
  const base = path.join(skillDir, "SKILL.md.v");
  let n = 1;
  while (true) {
    const candidate = `${base}${n}.bak`;
    try {
      await fs.stat(candidate);
      n += 1;
    } catch { return candidate; }
  }
}

export async function evolveSkill(nameOrSlug, { force = false, runs = 10, autoPromote = true, input = null } = {}) {
  const g = await isEvolvable(nameOrSlug);
  if (!g.ok) return { status: "skipped", reason: g.reason };

  const skillDir = g.dir;
  const skillMd = path.join(skillDir, "SKILL.md");
  const currentBody = await fs.readFile(skillMd, "utf8");
  const nameMatch = currentBody.match(/^name:\s*([^\n]+)/im);
  const descMatch = currentBody.match(/description:\s*([^\n]+)/i);
  const name = (nameMatch?.[1] || nameOrSlug).trim();
  const desc = (descMatch?.[1] || "").trim();

  // Telemetry check
  const rows = await telemetryRead({ skill: nameOrSlug });
  const stats = summarize(rows);
  const trig = shouldTrigger(stats);
  if (!force && !trig.trigger) {
    return { status: "not_triggered", stats, reason: trig.reason };
  }

  const failureRows = rows.filter((r) => !r.success).slice(-5).map((r) => r.error || `non-empty-output=${r.output_len > 0}`);

  // (b) rewriter
  let candidateBody;
  try {
    candidateBody = await runClaude(REWRITER_PROMPT(name, desc, currentBody, stats, failureRows), {
      timeoutMs: 180_000,
    });
  } catch (e) {
    return { status: "rewrite_failed", error: String(e?.message || e) };
  }
  let cleaned = candidateBody.trim();
  if (cleaned.startsWith("```")) {
    cleaned = cleaned.replace(/^```[a-z]*\n?/i, "").replace(/```$/i, "").trim();
  }
  if (!cleaned.startsWith("---") || !/name:\s*/.test(cleaned) || !/description:\s*/.test(cleaned)) {
    return { status: "rewrite_invalid", preview: cleaned.slice(0, 300) };
  }

  const candidatePath = `${skillMd}.candidate`;
  await fs.writeFile(candidatePath, cleaned, "utf8");

  // (d) A/B
  const abInput = input || desc || nameOrSlug;
  const currentResults = [];
  const candidateResults = [];
  for (let i = 0; i < runs; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    currentResults.push(await benchOnce(nameOrSlug, abInput, {
      bodyOverride: currentBody, nameOverride: name, turnId: `ab-cur-${Date.now()}-${i}`,
    }));
    // eslint-disable-next-line no-await-in-loop
    candidateResults.push(await benchOnce(nameOrSlug, abInput, {
      bodyOverride: cleaned, nameOverride: name, turnId: `ab-cand-${Date.now()}-${i}`,
    }));
  }
  const curStats = summarize(currentResults);
  const candStats = summarize(candidateResults);
  const won = betterThan(candStats, curStats);

  const decision = {
    skill: nameOrSlug,
    trigger_reason: trig.reason,
    current: curStats,
    candidate: candStats,
    promoted: false,
    at: nowIso(),
  };

  if (won && autoPromote) {
    const bak = await nextBackupPath(skillDir);
    await fs.rename(skillMd, bak);
    await fs.rename(candidatePath, skillMd);
    decision.promoted = true;
    decision.backup = bak;
  } else {
    // discard candidate unless caller wants to inspect it
    try { await fs.unlink(candidatePath); } catch {}
  }

  try { await memStore(NS_EVOLUTION, `evo:${nameOrSlug}:${Date.now()}`, decision); } catch {}
  await evolutionWrite(decision);
  return { status: won ? (autoPromote ? "promoted" : "candidate_won_not_promoted") : "discarded", ...decision };
}

export async function statsCommand({ skill } = {}) {
  if (skill) {
    const rows = await telemetryRead({ skill });
    return { skill, ...summarize(rows) };
  }
  const all = await telemetryRead();
  const bySkill = new Map();
  for (const r of all) {
    if (!bySkill.has(r.skill)) bySkill.set(r.skill, []);
    bySkill.get(r.skill).push(r);
  }
  return Array.from(bySkill.entries()).map(([s, rows]) => ({ skill: s, ...summarize(rows) }));
}

export async function reportCommand({ limit = 10 } = {}) {
  const all = await statsCommand();
  const arr = Array.isArray(all) ? all : [all];
  arr.sort((a, b) => (b.p95 ?? 0) - (a.p95 ?? 0));
  const slow = arr.filter((r) => (r.p95 ?? 0) > 3000 || (r.success_rate ?? 1) < 0.8).slice(0, limit);
  return { top_slow_or_failing: slow, all: arr };
}

export async function scanAndEvolveAll(opts = {}) {
  const names = await listGeneratedSkills();
  const out = [];
  for (const n of names) {
    // eslint-disable-next-line no-await-in-loop
    out.push({ skill: n, result: await evolveSkill(n, opts) });
  }
  return out;
}

// --- CLI entry ---------------------------------------------------------------
if (import.meta.url === `file://${process.argv[1]}`) {
  const argv = process.argv.slice(2);
  const opts = { runs: 10, autoPromote: false, force: false };
  let skill = null;
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--force") opts.force = true;
    else if (a === "--auto-promote") opts.autoPromote = true;
    else if (a === "--runs") opts.runs = Number.parseInt(argv[++i], 10) || 10;
    else if (a === "--input") opts.input = argv[++i];
    else if (!skill) skill = a;
  }
  if (!skill) {
    process.stderr.write("usage: hope_skill_evolve.mjs <skill> [--force] [--auto-promote] [--runs N] [--input \"text\"]\n");
    process.exit(2);
  }
  evolveSkill(skill, opts)
    .then((r) => {
      process.stdout.write(JSON.stringify(r, null, 2) + "\n");
      process.exit(0);
    })
    .catch((e) => {
      process.stdout.write(JSON.stringify({ status: "error", error: String(e?.message || e) }) + "\n");
      process.exit(1);
    });
}
