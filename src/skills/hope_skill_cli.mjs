#!/usr/bin/env node
// hope_skill_cli.mjs — unified entry for the hope-skill CLI.
//
// Subcommands:
//   create <description>         — on-demand skill creation
//   bench <skill> [--input X] [--runs N]
//   evolve <skill> [--force] [--auto-promote] [--runs N] [--input X]
//   stats [<skill>]              — p50/p95/success-rate summary
//   report [--limit N]           — top slow/failing skills
//   scan-evolve [--auto-promote] — walk all generated skills, evolve the
//                                  triggered ones (background-runner entry)

import { hopeSkillCreate } from "./hope_skill_create.mjs";
import { benchMany } from "./hope_skill_bench.mjs";
import {
  evolveSkill,
  statsCommand,
  reportCommand,
  scanAndEvolveAll,
} from "./hope_skill_evolve.mjs";

function usage() {
  return [
    "hope-skill — Hope's self-evolving skill system",
    "",
    "  hope-skill create <description>",
    "  hope-skill bench  <skill> [--input \"text\"] [--runs N]",
    "  hope-skill evolve <skill> [--force] [--auto-promote] [--runs N] [--input \"text\"]",
    "  hope-skill stats  [<skill>]",
    "  hope-skill report [--limit N]",
    "  hope-skill scan-evolve [--auto-promote]",
  ].join("\n");
}

function parseFlags(argv) {
  const flags = {};
  const positional = [];
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const name = a.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        flags[name] = next;
        i += 1;
      } else {
        flags[name] = true;
      }
    } else {
      positional.push(a);
    }
  }
  return { flags, positional };
}

async function main() {
  const [, , sub, ...rest] = process.argv;
  if (!sub || sub === "-h" || sub === "--help") {
    process.stderr.write(usage() + "\n");
    process.exit(sub ? 0 : 2);
  }
  const { flags, positional } = parseFlags(rest);
  let out;
  try {
    switch (sub) {
      case "create": {
        const description = positional.join(" ").trim();
        if (!description) throw new Error("create requires <description>");
        out = await hopeSkillCreate(description, {
          force: Boolean(flags.force),
          skipIndex: Boolean(flags["skip-index"]),
        });
        break;
      }
      case "bench": {
        const skill = positional[0];
        if (!skill) throw new Error("bench requires <skill>");
        const runs = Number.parseInt(flags.runs, 10) || 1;
        const input = typeof flags.input === "string" ? flags.input : null;
        const rows = await benchMany(skill, input, runs);
        out = { skill, runs: rows.length, results: rows };
        break;
      }
      case "evolve": {
        const skill = positional[0];
        if (!skill) throw new Error("evolve requires <skill>");
        out = await evolveSkill(skill, {
          force: Boolean(flags.force),
          autoPromote: Boolean(flags["auto-promote"]),
          runs: Number.parseInt(flags.runs, 10) || 10,
          input: typeof flags.input === "string" ? flags.input : null,
        });
        break;
      }
      case "stats": {
        const skill = positional[0];
        out = await statsCommand({ skill });
        break;
      }
      case "report": {
        out = await reportCommand({ limit: Number.parseInt(flags.limit, 10) || 10 });
        break;
      }
      case "scan-evolve": {
        out = await scanAndEvolveAll({ autoPromote: Boolean(flags["auto-promote"]) });
        break;
      }
      default:
        throw new Error(`unknown subcommand: ${sub}`);
    }
  } catch (e) {
    process.stdout.write(JSON.stringify({ status: "error", error: String(e?.message || e) }) + "\n");
    process.exit(1);
  }
  process.stdout.write(JSON.stringify(out, null, 2) + "\n");
  process.exit(0);
}

main();
