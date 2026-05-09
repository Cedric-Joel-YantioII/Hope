// auto-research.mjs — memory-first, web-fallback research for Hope.
// Flow: AgentDB + Claude-memory → short-circuit if topScore >= threshold,
// else web search + fetch + summarise → persist answer to AgentDB + JSON cache.
// Silent by default; the CLI wrapper decides whether to print JSON.

import { spawnSync } from "node:child_process";
import { readFile, readdir, writeFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export const MEMORY_HIT_THRESHOLD = 0.75;
export const CF_BIN = "/opt/homebrew/bin/claude-flow";
export const CLAUDE_MEMORY_DIR = join(
  homedir(),
  ".claude/projects/-Users-joelc/memory",
);
export const RESEARCH_NAMESPACE = "hope-research";
export const RESEARCH_CACHE_DIR = join(
  homedir(),
  "Documents/Github/Hope/.claude-flow/research-cache",
);

// ---------- helpers ----------

export function slugify(query) {
  return query
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 72);
}

function isoStamp() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

/**
 * Score a Claude-memory markdown file against a query using cheap
 * lexical overlap. Returns [0, 1]. Good enough as a fallback signal
 * when the vector index is unavailable.
 */
function lexicalScore(queryTerms, haystack) {
  if (!haystack) return 0;
  const text = haystack.toLowerCase();
  let hits = 0;
  for (const t of queryTerms) {
    if (!t) continue;
    if (text.includes(t)) hits += 1;
  }
  return queryTerms.length ? hits / queryTerms.length : 0;
}

function tokenise(query) {
  return query
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter((t) => t.length >= 3);
}

// ---------- memory layer ----------

/**
 * Search claude-flow's AgentDB via the CLI. Returns normalised results:
 *   [{ source: "agentdb", key, value, score }, ...]
 * Silent — swallows CLI noise and errors.
 */
export function searchAgentDB(query, { topK = 5 } = {}) {
  const out = spawnSync(
    CF_BIN,
    ["memory", "search", "-q", query, "--format", "json"],
    { encoding: "utf8", timeout: 15000 },
  );
  if (out.status !== 0 || !out.stdout) return [];
  // CLI prints an [INFO] line before the JSON blob, so grab the first {…}.
  const match = out.stdout.match(/\{[\s\S]*\}$/m);
  if (!match) return [];
  try {
    const parsed = JSON.parse(match[0]);
    const raw = Array.isArray(parsed.results) ? parsed.results : [];
    return raw.slice(0, topK).map((r) => ({
      source: "agentdb",
      key: r.key ?? r.id ?? "",
      value: r.value ?? r.text ?? r.content ?? "",
      score: typeof r.score === "number" ? r.score : r.similarity ?? 0,
    }));
  } catch {
    return [];
  }
}

/**
 * Search Claude Code's local memory files (~/.claude/projects/.../memory/*.md).
 * These are source-of-truth for Joel's personal memory and don't require
 * the AgentDB bridge to be live. Returns [{ source, key, value, score }, ...].
 */
export async function searchClaudeMemory(query, { topK = 5 } = {}) {
  if (!existsSync(CLAUDE_MEMORY_DIR)) return [];
  const terms = tokenise(query);
  if (!terms.length) return [];
  let files;
  try {
    files = await readdir(CLAUDE_MEMORY_DIR);
  } catch {
    return [];
  }
  const hits = [];
  for (const name of files) {
    if (!name.endsWith(".md")) continue;
    const path = join(CLAUDE_MEMORY_DIR, name);
    let body;
    try {
      body = await readFile(path, "utf8");
    } catch {
      continue;
    }
    const score = lexicalScore(terms, body);
    if (score <= 0) continue;
    hits.push({
      source: "claude-memory",
      key: name,
      value: body.trim().slice(0, 4000),
      score,
      path,
    });
  }
  hits.sort((a, b) => b.score - a.score);
  return hits.slice(0, topK);
}

/**
 * Unified memory search. Returns { hits, topScore }.
 * Merges AgentDB + Claude-memory hits, sorts by score descending.
 */
export async function searchMemory(query, opts = {}) {
  const [agentHits, claudeHits] = await Promise.all([
    Promise.resolve(searchAgentDB(query, opts)),
    searchClaudeMemory(query, opts),
  ]);
  const hits = [...agentHits, ...claudeHits].sort(
    (a, b) => b.score - a.score,
  );
  const topScore = hits.length ? hits[0].score : 0;
  return { hits: hits.slice(0, opts.topK ?? 5), topScore };
}

// ---------- web layer ----------

/**
 * DuckDuckGo Instant Answer first, then DDG HTML scrape fallback.
 * We avoid paid APIs and keep everything local/free.
 * Returns an array of { title, url, snippet }.
 */
export async function webSearch(query, { topK = 5 } = {}) {
  const results = [];
  const UA =
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) hope-research/0.1";

  // Pass 1: DDG Instant Answer API (curated, often empty for novel queries).
  try {
    const res = await fetch(
      `https://api.duckduckgo.com/?q=${encodeURIComponent(query)}&format=json&no_html=1&skip_disambig=1`,
      { headers: { "User-Agent": UA } },
    );
    if (res.ok) {
      const data = await res.json();
      if (data.AbstractText && data.AbstractURL) {
        results.push({
          title: data.Heading || query,
          url: data.AbstractURL,
          snippet: data.AbstractText,
        });
      }
      for (const topic of data.RelatedTopics ?? []) {
        if (topic.FirstURL && topic.Text) {
          results.push({
            title: topic.Text.slice(0, 120),
            url: topic.FirstURL,
            snippet: topic.Text,
          });
        }
        if (Array.isArray(topic.Topics)) {
          for (const sub of topic.Topics) {
            if (sub.FirstURL && sub.Text) {
              results.push({
                title: sub.Text.slice(0, 120),
                url: sub.FirstURL,
                snippet: sub.Text,
              });
            }
          }
        }
      }
    }
  } catch {
    /* silent */
  }

  // Pass 2: DDG HTML endpoint — parses the no-JS search page.
  // Only used when instant-answer returned nothing.
  if (results.length === 0) {
    try {
      const res = await fetch(
        `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`,
        { headers: { "User-Agent": UA } },
      );
      if (res.ok) {
        const html = await res.text();
        const linkRe =
          /class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/g;
        const snippetRe =
          /class="result__snippet"[^>]*>([\s\S]*?)<\/a>/g;
        const links = [...html.matchAll(linkRe)];
        const snippets = [...html.matchAll(snippetRe)];
        for (let i = 0; i < links.length; i += 1) {
          let rawUrl = links[i][1].replace(/&amp;/g, "&");
          const uddg = rawUrl.match(/uddg=([^&]+)/);
          if (uddg) {
            try {
              rawUrl = decodeURIComponent(uddg[1]);
            } catch {
              /* keep raw */
            }
          }
          if (!rawUrl.startsWith("http")) continue;
          const title = links[i][2]
            .replace(/<[^>]+>/g, "")
            .replace(/&#x27;/g, "'")
            .replace(/&amp;/g, "&")
            .replace(/\s+/g, " ")
            .trim();
          const snippet = (snippets[i]?.[1] ?? "")
            .replace(/<[^>]+>/g, "")
            .replace(/&#x27;/g, "'")
            .replace(/&amp;/g, "&")
            .replace(/\s+/g, " ")
            .trim();
          if (!title) continue;
          results.push({ title, url: rawUrl, snippet });
          if (results.length >= topK * 3) break;
        }
      }
    } catch {
      /* silent */
    }
  }

  // Dedup by URL, cap.
  const seen = new Set();
  const deduped = [];
  for (const r of results) {
    if (seen.has(r.url)) continue;
    seen.add(r.url);
    deduped.push(r);
    if (deduped.length >= topK) break;
  }
  return deduped;
}

/**
 * Fetch a URL and extract its readable text. No JS execution — just HTML
 * strip. Good enough for wikipedia-style summaries that DDG surfaces.
 */
export async function webFetch(url, { maxChars = 4000, timeoutMs = 12000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      headers: { "User-Agent": "hope-research/0.1 (+local)" },
      signal: ctrl.signal,
    });
    if (!res.ok) return "";
    const html = await res.text();
    // Strip scripts/styles, then tags, then collapse whitespace.
    const stripped = html
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    return stripped.slice(0, maxChars);
  } catch {
    return "";
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Cheap extractive summariser. Picks the top sentences by term-overlap
 * with the query. No LLM required — keeps the module self-contained.
 */
export function summarise(query, chunks, { maxSentences = 6 } = {}) {
  const terms = tokenise(query);
  const sentences = chunks
    .join(" ")
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 40 && s.length < 400);

  const scored = sentences.map((s) => ({
    s,
    score: lexicalScore(terms, s),
  }));
  scored.sort((a, b) => b.score - a.score);
  const chosen = scored
    .slice(0, maxSentences)
    .filter((x) => x.score > 0)
    .map((x) => x.s);
  return chosen.join(" ");
}

// ---------- persistence ----------

/**
 * Persist a synthesised answer back into AgentDB and to a local JSON
 * cache (so next call — even if AgentDB is empty — can find it via
 * the Claude-memory search path).
 */
export async function persistAnswer(query, answer, sources) {
  const slug = slugify(query);
  const key = `${slug}--${isoStamp()}`;
  const payload = {
    query,
    answer,
    sources,
    stored_at: new Date().toISOString(),
  };
  const value = JSON.stringify(payload);

  // Best-effort AgentDB write (silent on failure).
  spawnSync(
    CF_BIN,
    [
      "memory",
      "store",
      "-k",
      key,
      "--value",
      value,
      "--namespace",
      RESEARCH_NAMESPACE,
    ],
    { encoding: "utf8", timeout: 15000 },
  );

  // Local JSON cache — authoritative for next-run hits.
  try {
    await mkdir(RESEARCH_CACHE_DIR, { recursive: true });
    const cachePath = join(RESEARCH_CACHE_DIR, `${key}.json`);
    await writeFile(cachePath, value, "utf8");
    return { key, namespace: RESEARCH_NAMESPACE, cachePath };
  } catch {
    return { key, namespace: RESEARCH_NAMESPACE, cachePath: null };
  }
}

/**
 * Search the local research JSON cache (the one persistAnswer writes to).
 * Mirrors the claude-memory lexical score so we stay inside the unified
 * scoring regime.
 */
export async function searchResearchCache(query, { topK = 3 } = {}) {
  if (!existsSync(RESEARCH_CACHE_DIR)) return [];
  const terms = tokenise(query);
  if (!terms.length) return [];
  let files;
  try {
    files = await readdir(RESEARCH_CACHE_DIR);
  } catch {
    return [];
  }
  const hits = [];
  for (const name of files) {
    if (!name.endsWith(".json")) continue;
    const path = join(RESEARCH_CACHE_DIR, name);
    let body;
    try {
      body = await readFile(path, "utf8");
    } catch {
      continue;
    }
    const score = lexicalScore(terms, body);
    if (score <= 0) continue;
    try {
      const parsed = JSON.parse(body);
      hits.push({
        source: "research-cache",
        key: name,
        value: parsed.answer ?? body,
        score,
        path,
        meta: { sources: parsed.sources, stored_at: parsed.stored_at },
      });
    } catch {
      // skip malformed entries
    }
  }
  hits.sort((a, b) => b.score - a.score);
  return hits.slice(0, topK);
}

// ---------- orchestrator ----------

/**
 * Main entry point. Returns:
 *   {
 *     query,
 *     route: "memory" | "web",
 *     top_score,
 *     answer,               // synthesised or top memory value
 *     hits,                 // ordered memory/web hits used
 *     persisted: {...}|null
 *   }
 */
export async function autoResearch(query, opts = {}) {
  const threshold = opts.threshold ?? MEMORY_HIT_THRESHOLD;
  const topK = opts.topK ?? 5;

  // Gather from all local memory stores in parallel.
  const [agentHits, claudeHits, cacheHits] = await Promise.all([
    Promise.resolve(searchAgentDB(query, { topK })),
    searchClaudeMemory(query, { topK }),
    searchResearchCache(query, { topK }),
  ]);
  const memoryHits = [...agentHits, ...claudeHits, ...cacheHits].sort(
    (a, b) => b.score - a.score,
  );
  const topScore = memoryHits.length ? memoryHits[0].score : 0;

  if (topScore >= threshold) {
    const top = memoryHits[0];
    return {
      query,
      route: "memory",
      top_score: topScore,
      answer: top.value,
      hits: memoryHits.slice(0, topK),
      persisted: null,
    };
  }

  // Web fallback.
  const searchResults = await webSearch(query, { topK });
  const toFetch = searchResults.slice(0, 2);
  const fetched = await Promise.all(
    toFetch.map(async (r) => ({
      ...r,
      body: await webFetch(r.url),
    })),
  );
  const corpus = [
    ...searchResults.map((r) => r.snippet || ""),
    ...fetched.map((f) => f.body || ""),
  ];
  let answer = summarise(query, corpus, { maxSentences: 6 });
  if (!answer) {
    // Fall back to the top 2 snippets concatenated — better than nothing.
    answer = searchResults
      .slice(0, 2)
      .map((r) => r.snippet)
      .filter(Boolean)
      .join(" ")
      .trim();
  }
  if (!answer && fetched.length) {
    // Last resort: first 600 chars of the first fetched body.
    answer = (fetched[0].body || "").slice(0, 600);
  }

  const sources = searchResults.map((r) => ({
    title: r.title,
    url: r.url,
    fetched: toFetch.some((t) => t.url === r.url),
  }));

  const persisted = answer
    ? await persistAnswer(query, answer, sources)
    : null;

  return {
    query,
    route: "web",
    top_score: topScore,
    answer,
    hits: [
      ...memoryHits.slice(0, 3),
      ...sources.map((s, i) => ({
        source: "web",
        key: s.url,
        value: searchResults[i]?.snippet ?? "",
        score: null,
      })),
    ],
    persisted,
  };
}
