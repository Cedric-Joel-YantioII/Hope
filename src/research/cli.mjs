#!/usr/bin/env node
/**
 * hope-research CLI — thin wrapper around auto-research.mjs.
 *
 * Usage:
 *   node cli.mjs "query here"
 *   node cli.mjs --threshold 0.8 "query here"
 *   node cli.mjs --pretty "query here"
 *
 * Silent-by-default: prints ONE JSON object on stdout, nothing on stderr
 * unless --verbose. Designed to be piped into other tools.
 */

import { autoResearch } from "./auto-research.mjs";

function parseArgs(argv) {
  const opts = { pretty: false, verbose: false };
  const rest = [];
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--pretty") opts.pretty = true;
    else if (a === "--verbose") opts.verbose = true;
    else if (a === "--threshold") {
      opts.threshold = Number.parseFloat(argv[++i]);
    } else if (a === "--top-k") {
      opts.topK = Number.parseInt(argv[++i], 10);
    } else if (a === "--help" || a === "-h") {
      opts.help = true;
    } else {
      rest.push(a);
    }
  }
  opts.query = rest.join(" ").trim();
  return opts;
}

function usage() {
  return [
    "hope-research — memory-first, web-fallback research",
    "",
    "Usage:",
    "  hope-research [--threshold 0.75] [--top-k 5] [--pretty] [--verbose] \"query\"",
    "",
    "Output: single JSON object on stdout:",
    "  { query, route: \"memory\"|\"web\", top_score, answer, hits, persisted }",
  ].join("\n");
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help || !opts.query) {
    process.stderr.write(usage() + "\n");
    process.exit(opts.help ? 0 : 2);
  }

  const started = Date.now();
  try {
    const result = await autoResearch(opts.query, {
      threshold: opts.threshold,
      topK: opts.topK,
    });
    result.elapsed_ms = Date.now() - started;
    process.stdout.write(
      JSON.stringify(result, null, opts.pretty ? 2 : 0) + "\n",
    );
    process.exit(0);
  } catch (err) {
    process.stdout.write(
      JSON.stringify({
        query: opts.query,
        route: "error",
        error: err?.message ?? String(err),
      }) + "\n",
    );
    process.exit(1);
  }
}

main();
