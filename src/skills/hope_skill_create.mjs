// hope_skill_create.mjs — on-demand skill creation for Hope.
//
// Flow (per Joel's spec):
//   (a) Check existing skills at ~/.claude/skills/*/SKILL.md via `hope-research`
//       scoped to namespace `skills-index`. If semantic match >= 0.8 -> return
//       "already exists: <name>".
//   (b) Spawn `claude -p` with a generator prompt to produce SKILL.md.
//   (c) Write to ~/Documents/Github/Hope/src/skills/generated/<slug>/SKILL.md
//       and symlink into ~/.claude/skills/<slug>.
//   (d) Self-test: spawn `claude -p` to invoke the skill against an
//       auto-generated test case; record pass/fail to `hope-skills-eval`.
//
// Never overwrites hand-written skills. If a symlink collision exists we bail
// with collision=true. All LLM work goes through `claude -p` (Max auth).

import { promises as fs } from "node:fs";
import path from "node:path";
import {
  GENERATED_DIR,
  SKILLS_LINK_DIR,
  NS_INDEX,
  NS_EVAL,
  slug,
  runCmd,
  runClaude,
  memStore,
  nowIso,
} from "./hope_skill_common.mjs";

const GENERATOR_PROMPT = (desc) => `You are generating a Claude Code Skill.

Produce a complete SKILL.md file that teaches Claude how to perform this capability:

"""
${desc}
"""

Requirements:
- Start with YAML frontmatter containing exactly two fields: "name" and "description".
- "name" MUST be a short kebab-case slug (no spaces, lowercase).
- "description" is a one-sentence trigger — what the user has to ask for to warrant invoking this skill.
- Body: short markdown explaining how to perform the capability step-by-step, any CLI calls, expected inputs/outputs, and when to invoke.
- Keep the total under 80 lines.
- Output ONLY the raw markdown (frontmatter + body). No backticks, no preamble, no trailing commentary.`;

const TEST_GEN_PROMPT = (desc) => `Generate ONE concrete test case the skill below should handle end-to-end. Output ONLY the test input — one line, no quotes, no commentary.

Capability: ${desc}`;

const INVOKE_PROMPT = (name, testCase, skillBody) => `The following is a Claude Code Skill definition. Invoke it mentally and produce the output it would yield on this test case. At the very end of your response, on its own line, print one of: PASS or FAIL.

--- SKILL (${name}) ---
${skillBody}
--- END SKILL ---

TEST INPUT:
${testCase}`;

// Lexical Jaccard-like similarity over token sets + bigrams. This is NOT a
// neural embedding match — we intentionally use a deterministic, local measure
// because the claude-flow `memory search --namespace` subcommand is currently
// unreliable across DB handles. The skills-index namespace write still happens
// (indexExistingSkills) so any future MCP-native semantic consumer can use it.
function tokenize(s) {
  return String(s).toLowerCase().replace(/[^a-z0-9 ]+/g, " ").split(/\s+/).filter(Boolean);
}
function bigrams(tokens) {
  const out = [];
  for (let i = 0; i < tokens.length - 1; i += 1) out.push(`${tokens[i]} ${tokens[i + 1]}`);
  return out;
}
function jaccard(a, b) {
  const A = new Set(a); const B = new Set(b);
  if (!A.size || !B.size) return 0;
  let inter = 0;
  for (const x of A) if (B.has(x)) inter += 1;
  return inter / (A.size + B.size - inter);
}
function similarity(descA, descB) {
  const ta = tokenize(descA); const tb = tokenize(descB);
  const unigrams = jaccard(ta, tb);
  const bi = jaccard(bigrams(ta), bigrams(tb));
  return 0.6 * unigrams + 0.4 * bi;
}

async function semanticMatchExisting(description) {
  // Walk ~/.claude/skills/*/SKILL.md, score each description against the
  // incoming capability. Top match >= 0.8 blocks creation.
  let best = { score: 0, name: null };
  const entries = await fs.readdir(SKILLS_LINK_DIR, { withFileTypes: true });
  for (const e of entries) {
    if (!e.isDirectory() && !e.isSymbolicLink()) continue;
    const mdPath = path.join(SKILLS_LINK_DIR, e.name, "SKILL.md");
    try {
      const body = await fs.readFile(mdPath, "utf8");
      const m = body.match(/description:\s*([^\n]+)/i);
      const d = (m?.[1] || "").trim();
      if (!d) continue;
      // Score against description and also against "<name> — <description>"
      // so a query that matches the slug directly still wins.
      const s = Math.max(similarity(description, d), similarity(description, `${e.name} ${d}`));
      if (s > best.score) best = { score: s, name: e.name };
    } catch {}
  }
  return best;
}

async function indexExistingSkills() {
  // One-shot: walk ~/.claude/skills/*/SKILL.md, push their frontmatter to the
  // skills-index namespace so semantic search has embedded content to match.
  let indexed = 0;
  const entries = await fs.readdir(SKILLS_LINK_DIR, { withFileTypes: true });
  for (const e of entries) {
    if (!e.isDirectory() && !e.isSymbolicLink()) continue;
    const skillMd = path.join(SKILLS_LINK_DIR, e.name, "SKILL.md");
    try {
      const body = await fs.readFile(skillMd, "utf8");
      // extract frontmatter description
      const m = body.match(/description:\s*([^\n]+)/i);
      const description = (m?.[1] || "").trim();
      if (!description) continue;
      await memStore(NS_INDEX, `skill:${e.name}`, `${e.name} — ${description}`);
      indexed += 1;
    } catch {}
  }
  return indexed;
}

async function generateSkill(description) {
  const md = await runClaude(GENERATOR_PROMPT(description), { timeoutMs: 120_000 });
  // strip accidental code-fence wrap
  let cleaned = md.trim();
  if (cleaned.startsWith("```")) {
    cleaned = cleaned.replace(/^```[a-z]*\n?/i, "").replace(/```$/i, "").trim();
  }
  if (!cleaned.startsWith("---")) {
    throw new Error("generator did not return frontmatter: " + cleaned.slice(0, 200));
  }
  return cleaned;
}

async function selfTest(name, description, skillBody) {
  let testCase = "";
  try {
    testCase = (await runClaude(TEST_GEN_PROMPT(description), { timeoutMs: 60_000 }))
      .split("\n")[0]
      .trim();
  } catch {
    testCase = description;
  }
  let output = "";
  let pass = false;
  let err = null;
  try {
    output = await runClaude(INVOKE_PROMPT(name, testCase, skillBody), { timeoutMs: 120_000 });
    pass = /\bPASS\b\s*$/m.test(output);
  } catch (e) {
    err = String(e?.message || e);
  }
  return { testCase, output, pass, error: err };
}

export async function hopeSkillCreate(description, { skipIndex = false, force = false } = {}) {
  if (!description || !description.trim()) {
    throw new Error("description required");
  }
  const name = slug(description);
  const genDir = path.join(GENERATED_DIR, name);
  const linkPath = path.join(SKILLS_LINK_DIR, name);

  // Index existing skills (idempotent — just upserts descriptions)
  let indexed = 0;
  if (!skipIndex) {
    try { indexed = await indexExistingSkills(); } catch {}
  }

  // (a) Semantic match
  const match = await semanticMatchExisting(description);
  if (!force && match.score >= 0.8 && match.name) {
    return {
      status: "already_exists",
      name: match.name,
      score: match.score,
      message: `already exists: ${match.name}`,
    };
  }

  // Collision check: never overwrite a hand-written skill
  try {
    const st = await fs.lstat(linkPath);
    if (st) {
      const isSym = st.isSymbolicLink();
      if (!isSym) {
        return {
          status: "collision",
          name,
          message: `a non-symlink directory already exists at ${linkPath}; refusing to overwrite`,
        };
      }
      const real = await fs.realpath(linkPath);
      if (!real.startsWith(GENERATED_DIR)) {
        return {
          status: "collision",
          name,
          message: `${linkPath} points to hand-written ${real}; refusing to overwrite`,
        };
      }
    }
  } catch (err) {
    if (err.code !== "ENOENT") throw err;
  }

  // (b) Generate
  const body = await generateSkill(description);

  // (c) Write + symlink
  await fs.mkdir(genDir, { recursive: true });
  const skillMdPath = path.join(genDir, "SKILL.md");
  await fs.writeFile(skillMdPath, body, "utf8");

  // replace existing symlink if it was previously Hope-generated
  try { await fs.unlink(linkPath); } catch {}
  await fs.symlink(genDir, linkPath, "dir");

  // re-index
  try {
    const descMatch = body.match(/description:\s*([^\n]+)/i);
    const desc = (descMatch?.[1] || description).trim();
    await memStore(NS_INDEX, `skill:${name}`, `${name} — ${desc}`);
  } catch {}

  // (d) Self-test
  const test = await selfTest(name, description, body);
  await memStore(NS_EVAL, `eval:${name}:${Date.now()}`, {
    skill: name,
    description,
    test_case: test.testCase,
    pass: test.pass,
    error: test.error,
    output_preview: (test.output || "").slice(0, 400),
    created_at: nowIso(),
  });

  return {
    status: "created",
    name,
    path: skillMdPath,
    symlink: linkPath,
    indexed_existing: indexed,
    pre_match_score: match.score,
    self_test: { pass: test.pass, test_case: test.testCase, error: test.error },
  };
}

// --- CLI entry ---------------------------------------------------------------
if (import.meta.url === `file://${process.argv[1]}`) {
  const args = process.argv.slice(2);
  const opts = { force: false };
  const rest = [];
  for (const a of args) {
    if (a === "--force") opts.force = true;
    else if (a === "--skip-index") opts.skipIndex = true;
    else rest.push(a);
  }
  const description = rest.join(" ").trim();
  if (!description) {
    process.stderr.write("usage: hope_skill_create.mjs [--force] [--skip-index] \"<description>\"\n");
    process.exit(2);
  }
  hopeSkillCreate(description, opts)
    .then((r) => {
      process.stdout.write(JSON.stringify(r, null, 2) + "\n");
      process.exit(r.status === "created" || r.status === "already_exists" ? 0 : 1);
    })
    .catch((e) => {
      process.stdout.write(JSON.stringify({ status: "error", error: String(e?.message || e) }) + "\n");
      process.exit(1);
    });
}
