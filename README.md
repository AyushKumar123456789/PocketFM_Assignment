# goagent

An agentic AI contributor for open-source Go projects. Give it a GitHub issue number from one of four approved Go repos and it produces a code change, runs `go build`/`vet`/`test`, and writes a PR title and body.

This is a take-home assignment submission. The goal isn't the LLM — it's the **framework** around the LLM: a deterministic seven-stage pipeline with a retry loop, structured edits (not raw diffs), and full per-run tracing.

```
Issue ──▶ Fetch ──▶ Index ──▶ Retrieve ──▶ Plan ──▶ Edit ──▶ Validate ──▶ Summarize ──▶ patch + pr.md
                                                       ▲           │
                                                       └── retry ──┘
```

📹 **Walkthrough video:** https://youtu.be/nIDCER8rM8Q?si=q9D0rKa72vR7xdGm

---

## Table of contents

- [Quick start (5 minutes)](#quick-start-5-minutes)
- [Architecture](#architecture)
- [Stages in detail](#stages-in-detail)
- [Retrieval &amp; ranking](#retrieval--ranking)
- [Commands reference](#commands-reference)
- [What gets written to disk](#what-gets-written-to-disk)
- [Supported LLM providers](#supported-llm-providers)
- [Supported target repos](#supported-target-repos)
- [Repo layout](#repo-layout)
- [Design decisions](#design-decisions)
- [Eval harness](#eval-harness)
- [What this submission deliberately does NOT include](#what-this-submission-deliberately-does-not-include)
- [Troubleshooting](#troubleshooting)

---

## Quick start (5 minutes)

### 1. Prerequisites

Install once, in any order. The agent fails fast with a clear message if any are missing.

| Tool | Why | Install |
|---|---|---|
| **Python 3.10+** | The agent itself | https://www.python.org/downloads/ |
| **Go 1.21+** | Run `go build` / `vet` / `test` on the target repo | https://go.dev/doc/install |
| **git** | Clone target repos | https://git-scm.com/ |
| **An LLM API key** | The agent calls one of: Gemini (free), Groq (free), Claude (paid), DeepSeek via NVIDIA NIM (free trial) | see [Supported LLM providers](#supported-llm-providers) |

Optional but recommended:
- **ripgrep** (`rg`) — speeds up file retrieval (pure-Python fallback if missing).
- **GitHub token** — higher rate limit when fetching issue text.

### 2. Install

```powershell
git clone <this-repo-url>
cd goagent
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # bash/zsh: source .venv/bin/activate
pip install -e .
```

### 3. Set your API key

```powershell
Copy-Item .env.example .env      # bash: cp .env.example .env
# Open .env and paste in your key. Minimum one line:
#   GEMINI_API_KEY=<your-key>
# Or if using Claude:
#   ANTHROPIC_API_KEY=<your-key>
#   GOAGENT_MODEL=claude-sonnet-4-6
```

### 4. Sanity check

```powershell
goagent --help                   # prints usage
goagent repos                    # lists 4 supported repos — confirms configs load
```

### 5. Run on one issue

```powershell
# Free / cheap dry-run (1 LLM call, stops after the Plan stage):
goagent solve cobra 2376 --dry-run

# Full pipeline (3-6 LLM calls, runs go build/test, writes a patch):
goagent solve cobra 2376
```

When done, look in `runs/<timestamp>_cobra_2376/`:
- `pr.md` — proposed PR title + body
- `changes.patch` — unified diff (apply with `git apply` inside `workspaces/cobra/`)
- `trace.jsonl` — every LLM call + every tool call (the agent's diary; read this first)

---

## Architecture

A linear seven-stage pipeline. The only place stage order is defined is `agent/loop.py`. Each box is one Python module under `agent/stages/`. (The CLI prints six numbered progress steps, not seven, because it folds workspace-prep into the Index step and Edit+Validate into one step.)

```
              ┌──────────────────────────────────────────────────────────┐
              │                                                          ▼
  Issue ──▶ Fetch ──▶ Index ──▶ Retrieve ──▶ Plan ──▶ Edit ──▶ Validate ─┴──▶ Summarize ──▶ patch + pr.md
            (REST)    (FS walk  (ripgrep +   (LLM:    (LLM:    (gofmt,        (LLM:
                       + regex   symbol      hypoth., structured vet, build,   title +
                       AST       score +     files,   edits)   test ./pkg/)   body)
                       cache)    rank)       tests)
```

| Stage | What it does | File | Uses LLM? |
|---|---|---|---|
| **Fetch** | `GET /repos/.../issues/N` from GitHub | `agent/stages/fetch_issue.py` | no |
| **Index** | Walk the cloned repo, parse `.go` files with regex, build a `RepoMap` of packages + symbols (cached per commit) | `agent/stages/index_repo.py` | no |
| **Retrieve** | ripgrep the workspace for identifiers from the issue text + symbol lookup. Score and rank candidate files. | `agent/stages/retrieve.py` | no |
| **Plan** | "Read the issue + repo map + candidates. Output JSON: hypothesis, files_to_edit, approach, risks." | `agent/stages/plan.py` | **yes** |
| **Edit** | "Read the plan + file contents. Output structured edit ops (replace/insert/create)." | `agent/stages/edit.py` | **yes** |
| **Validate** | `gofmt → go vet → go build → go test`. On fail, feed stderr back to Edit and retry. | `agent/stages/validate.py` | no |
| **Summarize** | "Write a PR title + body from the issue, plan, and validation result." | `agent/stages/summarize.py` | **yes** |

Three LLM calls per successful run (Plan, Edit, Summarize). Up to N extra Edit calls on validation failure (default 3).

---

## Stages in detail

The orchestrator (`agent/loop.py`) is the **only** place stage order is defined; each stage is a pure-ish function that takes its inputs and returns a concrete dataclass. Stages 1–3 are deterministic (no LLM); stages 4, 5, 7 each make one LLM call; stage 6 is deterministic and is the agent's correctness signal.

### 1. Fetch — `agent/stages/fetch_issue.py` · _no LLM_

Pulls the issue from the GitHub REST API (`GET /repos/{owner}/{repo}/issues/{N}`) and, if it has any, a second call for its comments. A `GITHUB_TOKEN` is used when present (higher rate limit) but isn't required. The result is an `Issue` dataclass; its `.text_blob` property concatenates title + body + each comment into one string — that's the single source the keyword extractor and the Planner prompt consume. A 404 raises a clear `FileNotFoundError` rather than failing deeper in the pipeline.

### 2. Index — `agent/stages/index_repo.py` · _no LLM_

Builds a **`RepoMap`**: a compact, token-frugal model of the repo injected into prompts in place of "here is the whole repo." It `rglob`s every `.go` file (skipping ignored globs and `_test.go` files, which are too noisy for the map but still readable by later tools) and runs an **AST-lite regex pass** — not `go`/`gopls`, to avoid that dependency — to extract per package:
- the `package X` declaration and a one-line purpose from the leading doc comment;
- every **exported** symbol (`func` / `type` / `var` / `const`, capitalized) with its file and line.

The map is **cached per commit** at `.goagent/repo_map_<sha>.json` inside the workspace, so repeated runs on the same checkout skip the walk. `short_summary()` renders it (capped at 30 symbols/package, 8000 chars) for prompt injection.

### 3. Retrieve — `agent/stages/retrieve.py` · _no LLM_

Selects the handful of files worth showing the LLM. This is the L1 half of the two-stage retriever — full details in [Retrieval &amp; ranking](#retrieval--ranking). In short: extract keywords from the issue, score files by lexical (ripgrep) + symbol-map signals, down-weight vendored/generated paths, and return the top 12 candidates with their scores and human-readable `reasons`.

### 4. Plan — `agent/stages/plan.py` · **LLM call**

One cheap, tool-free completion (`temperature=0`, `max_tokens=2048`). The system prompt carries the repo map + extracted [conventions](#design-decisions) + repo notes; the user message carries the issue and the ranked candidates. The model must return a **validated JSON** `Plan`:

```json
{
  "hypothesis": "one paragraph: the root cause",
  "files_to_edit": ["pkg/foo.go"],
  "files_to_read_first": ["pkg/bar.go"],
  "test_packages": ["./pkg/foo/..."],
  "approach": "outline of the change",
  "risks": ["what could break"]
}
```

This is also the **L2 reranker**: by choosing `files_to_edit` from the candidate set it prunes retrieval false-positives before the Editor opens a file. Parsing is tolerant of ```` ```json ```` fences and falls back to first-`{`/last-`}` extraction. `--dry-run` stops the pipeline here.

### 5. Edit — `agent/stages/edit.py` · **LLM call** (the retried one)

Reads the full contents of the planner's `files_to_edit` + `files_to_read_first` (budgeted to ~40k chars/file) and returns a JSON list of **structured edit operations**, never a raw diff:

```json
{"op":"replace","file":"foo.go","find":"<exact text>","replace":"<new text>"}
{"op":"insert_after","file":"foo.go","anchor":"<exact text>","content":"..."}
{"op":"insert_before","file":"foo.go","anchor":"<exact text>","content":"..."}
{"op":"create_file","file":"new.go","content":"..."}
```

Each `find` / `anchor` must match **exactly once** in the target — the applier (`agent/tools/edits.py`) rejects zero-match (hallucinated) and multi-match (ambiguous) edits before they become silent bugs. The actual unified diff is produced from `git diff` after ops apply. JSON parsing is hardened (fast path → fenced-block → brace-balanced, string-aware scan) so stray prose or shell `${var}` braces don't break extraction. On a retry the previous validation stderr is injected as `prior_error` with an instruction not to repeat the same edits.

### 6. Validate — `agent/stages/validate.py` · _no LLM_

The correctness gate, run after every Edit: **`gofmt` → `go vet` → `go build` → `go test`**. Tests are scoped first to the **affected packages** (fast feedback), and only if those pass does it run the **broad `./...` sweep** as a smoke test. Every command runs through a timeout-and-output-capped subprocess runner. The combined result is a `ValidationResult`; on failure its `error_summary()` (the relevant stage's stderr) becomes the next attempt's `prior_error`. `passed` requires all four stages green.

### 7. Summarize — `agent/stages/summarize.py` · **LLM call**

Takes the issue, plan, edit rationale, and validation result and emits JSON `{title, body}` for the PR. The body uses fixed sections (**Summary / Closes / Validation / Notes**) and follows the repo's commit style (e.g. lowercase imperative subjects). `Closes #<issue>` is force-appended if the model omits it. This stage is **best-effort**: if the LLM call fails (rate limit, network, malformed output), the orchestrator writes a **deterministic fallback** PR built from the plan + validation result so the reviewer always gets readable artifacts.

### The retry loop (orchestrator)

Stages 5–6 run inside a bounded loop (`--max-retries`, default 3). **Every** failure mode — JSON parse error, degenerate output, unapplicable edit, or any of gofmt/vet/build/test failing — funnels through the same path: capture the error, `git reset --hard HEAD` to start clean, feed the error back to the Editor as `prior_error`, and retry. After the loop, Summarize and the `changes.patch` write happen once regardless of success, and `run.end` is traced with duration, success, and attempt count.

---

## Retrieval &amp; ranking

Context selection follows the standard information-retrieval pattern of **two-stage retrieve-and-rerank** (a.k.a. candidate generation → reranking, or L1 retrieval + L2 ranking):

| Stage | Role | Optimizes for | Implementation |
|---|---|---|---|
| **L1 — Retrieve** | Cheap, high-recall candidate generation over the whole repo | **Recall** (don't miss the right file) | `agent/stages/retrieve.py` — sparse lexical + symbol scoring |
| **L2 — Rerank** | Expensive, high-precision pruning of the candidate set | **Precision** (drop the noise) | The Planner LLM (`agent/stages/plan.py`) |

This is the same architecture search engines use (a fast recall-oriented index pass, then a slower precision-oriented reranker), just with an LLM as the reranker instead of a learned ranking model.

### L1: sparse lexical + symbol retrieval

The retriever is deliberately **sparse** (term/symbol-based, BM25-family) rather than **dense** (embedding/vector-based) — no vector store, no chunking, no learned model. For focused GitHub issues the issue text almost always names the relevant identifiers, so lexical + symbol matching gets ~80% of the value at ~5% of the complexity. See [Design decisions](#design-decisions).

**Query construction (keyword extraction).** The issue title + body + comments are tokenized into two term sets:
- **General tokens** — alphanumeric identifiers of length > 2, lowercased, with a stopword list (`the`, `bug`, `error`, `go`, `func`, `package`, …) removed.
- **CamelCase identifiers** — tokens matching `[A-Z][a-zA-Z0-9]{2,}`, kept case-sensitive because they're the most likely Go symbol names.

**Scoring (weighted additive / linear fusion).** Three independent signals each contribute hits, and a file's score is the **sum of every signal that touched it** — a transparent, hand-tuned linear scoring function:

| # | Signal | What it does | Weight |
|---|---|---|---|
| 1 | **Symbol match** | A CamelCase token resolves to a `Symbol` (func/type/var/const) in the cached `RepoMap`. Strongest signal — a named symbol points straight at its defining file. | **+5.0** |
| 2 | **Lexical, CamelCase** | ripgrep each CamelCase identifier across the workspace. Likely Go symbols, so weighted high. | **+3.0** / file hit |
| 3 | **Lexical, lowercase token** | ripgrep the longest / most distinctive lowercase tokens (≥5 chars, top 20). Case-insensitive, capped to protect the search budget. | **+0.5** / file hit |

> *Neighbor expansion* (graph-style fan-out to files that import or are imported by a hit) is described in the module docstring as a planned signal; the shipping scorer uses signals 1–3.

### L1 ranking &amp; selection

After the signals accumulate, two adjustments and a sort produce the candidate list:

1. **Penalty down-weighting** — paths under `vendor/`, `/testdata/`, or matching `*_gen.go` are multiplied by **0.1** so generated/vendored code rarely outranks real source.
2. **Sort key** — `(-score, len(path))`: highest score first, and on a tie the **shorter path wins** (top-level files tend to be more relevant than deeply nested ones).
3. **Top-k cut** — the top **12** candidates (`top_n`) are returned, each carrying its score and up to 8 human-readable `reasons` (e.g. `sym:func ExecuteContext`, `camel:RunE@142`, `tok:completion`) for traceability.

### L2: LLM reranking

The ranked candidates are handed to the **Planner**, which reads them alongside the issue and repo map and emits the final `files_to_edit` set — pruning false positives before the **Editor** ever opens a file. Splitting recall (L1) from precision (L2) keeps the cheap pass aggressive without flooding the expensive pass with noise.

### Known limitations

- **Lexical blind spot** — an issue that describes a *behavior* without naming any identifier (no symbol/keyword overlap) retrieves poorly; this is the canonical case where dense retrieval would help.
- **Linear weights are hand-tuned**, not learned — the +5/+3/+0.5 split is a heuristic, not fit to labeled data.
- **No query expansion** — synonyms/abbreviations in the issue (e.g. "completion" vs "complete") aren't bridged beyond the case-insensitive lowercase pass.

---

## Commands reference

All commands are subcommands of `goagent`. Defined in `agent/cli.py` via argparse.

### `goagent solve <repo> <issue#>`

Run the full pipeline on one GitHub issue.

```powershell
goagent solve cobra 2376
goagent solve gin 4003 --model claude-sonnet-4-6
goagent solve validator 1289 --dry-run
goagent solve golangci-lint 5142 --max-retries 5
```

Flags:

| Flag | Default | Purpose |
|---|---|---|
| `--dry-run` | off | Stop after the Plan stage (1 LLM call). Cheap way to validate that the agent identified the right files. |
| `--model <name>` | `gemini-2.5-flash` | LLM to use. Provider is inferred from the prefix (`claude-`, `gemini-`, `groq/`, `deepseek-ai/`, `gpt-`). |
| `--max-retries <N>` | `3` | How many Edit→Validate cycles before giving up. |
| `--runs-dir <path>` | `runs` | Where to write the per-run artifact directory. |

### `goagent eval <fixtures.yaml>`

Score the agent against a list of `(repo, issue#, accepted_PR#)` tuples. For each fixture: run the agent end-to-end, then compute three metrics:
1. **Files Jaccard** — overlap between agent-changed files and the accepted PR's files.
2. **Tests passed?** — did the agent's diff leave `go test ./...` green?
3. **LLM-as-judge** — a separate LLM call (whatever `--model` you pass; uses the same provider abstraction as the agent, so Gemini or Claude both work) rates root-cause similarity 1–5.

```powershell
goagent eval evals\fixtures.yaml
```

Fixtures live in `evals/fixtures.yaml` (ships with placeholders — replace with real closed issue/PR numbers to actually score).

### `goagent repos`

List the configured target repos. Used to verify your install picked up the YAML configs.

```powershell
goagent repos
# cobra          — https://github.com/spf13/cobra.git
# gin            — https://github.com/gin-gonic/gin.git
# validator      — https://github.com/go-playground/validator.git
# golangci-lint  — https://github.com/golangci/golangci-lint.git
```

---

## What gets written to disk

Each `solve` run creates `runs/<UTC-timestamp>_<repo>_<issue#>/`:

```
runs/20260604T142301Z_cobra_2376/
├── issue.json          # raw GitHub issue + comments we fetched
├── repo_map.json       # indexed package tree + symbol locations
├── plan.json           # Planner output (hypothesis, files, approach, risks)
├── trace.jsonl         # every LLM call + every tool call, append-only
├── changes.patch       # unified diff (empty if Edit failed unrecoverably)
└── pr.md               # proposed PR title + body
```

**Read `trace.jsonl` first** — it's the agent's diary. One line per event with token counts, costs, and previews. For a real one you can read right now without running anything, see [`samples/cobra_2376/trace.jsonl`](samples/cobra_2376/trace.jsonl).

Apply a patch to the workspace:
```powershell
cd workspaces\cobra
git apply ..\..\runs\20260604T142301Z_cobra_2376\changes.patch
```

---

## Supported LLM providers

The provider is selected by the **model name prefix**. Set `GOAGENT_MODEL=<name>` in `.env` or pass `--model <name>` on the CLI. The matching `<PROVIDER>_API_KEY` must be set in `.env`.

| Prefix | Provider | Example model | Env var | Free tier? |
|---|---|---|---|---|
| `claude-` | Anthropic | `claude-sonnet-4-6`, `claude-opus-4-7` | `ANTHROPIC_API_KEY` | No (paid only) |
| `gemini-` | Google AI Studio | `gemini-2.5-flash`, `gemini-2.5-pro` | `GEMINI_API_KEY` | Yes — Flash has 1500 req/day free |
| `groq/` | Groq (Llama via LPU) | `groq/llama-3.3-70b-versatile`, `groq/meta-llama/llama-4-scout-17b-16e-instruct` | `GROQ_API_KEY` | Yes — generous req/day, tight TPM |
| `deepseek-ai/` | NVIDIA NIM | `deepseek-ai/deepseek-v4-pro` | `NVIDIA_API_KEY` | Limited free trial |
| `gpt-` | OpenAI | `gpt-4o-mini`, `gpt-4o` | `OPENAI_API_KEY` | No (paid only) |

Adding a new provider = adding one branch in `agent/llm.py:build_llm()` (~10 lines).

**Heads-up on Claude wall-clock time.** To stay under the free-tier Anthropic limit of 30k input tokens/minute (each Edit call is ~10k), `AnthropicLLM` **sleeps up to 60s between consecutive calls** (traced as `rate_limit.pause`). A Claude run that looks "hung" for a minute is almost always this deliberate pause, not a crash. Gemini and Groq have no such pause. See `agent/llm.py:_maybe_sleep_for_rate_limit`.

---

## Supported target repos

| Config name | Repo | Config file |
|---|---|---|
| `cobra` | spf13/cobra | `configs/cobra.yaml` |
| `gin` | gin-gonic/gin | `configs/gin.yaml` |
| `validator` | go-playground/validator | `configs/validator.yaml` |
| `golangci-lint` | golangci/golangci-lint | `configs/golangci-lint.yaml` |

Adding a fifth repo = adding one YAML file with clone URL, default branch, build/test/vet/fmt commands, and ignore globs.

---

## Repo layout

```
.
├── agent/                     # The agent itself
│   ├── cli.py                 # argparse entrypoint — defines `solve` / `eval` / `repos`
│   ├── loop.py                # orchestrator — the only place stage order is defined
│   ├── llm.py                 # provider abstraction (Claude / Gemini / Groq / DeepSeek / OpenAI)
│   ├── tracing.py             # append-only JSONL trace writer
│   ├── prompt_loader.py       # loads prompts/*.md
│   ├── conventions.py         # extracts CONTRIBUTING.md / .golangci.yml / commit style
│   ├── config.py              # per-repo YAML loader
│   ├── stages/                # one module per pipeline stage
│   │   ├── fetch_issue.py
│   │   ├── index_repo.py
│   │   ├── retrieve.py
│   │   ├── plan.py
│   │   ├── edit.py
│   │   ├── validate.py
│   │   └── summarize.py
│   └── tools/                 # reusable utilities, no agent knowledge
│       ├── fs.py              # safe path/file reads
│       ├── search.py          # ripgrep wrapper (with pure-Python fallback)
│       ├── edits.py           # structured-edit applier + sanity checks + revert
│       ├── shell.py           # subprocess runner with timeouts + output caps
│       ├── symbols.py         # regex symbol lookup over .go files
│       └── workspace.py       # clone/refresh the target repo
├── configs/                   # per-repo settings (one YAML each)
├── prompts/                   # versioned LLM prompts (plan.md, edit.md, summarize.md)
├── workspaces/                # cloned target repos live here (gitignored)
├── runs/                      # per-run artifacts (gitignored)
├── samples/                   # checked-in sample runs for reviewers
├── evals/                     # eval harness + fixtures
├── pyproject.toml
├── .env.example
└── README.md
```

---

## Design decisions

A few non-obvious choices, with the reasoning:

**Structured edits, not raw diffs.** The Editor LLM emits a JSON list of operations (`replace`, `insert_after`, `insert_before`, `create_file`), not a unified diff. Each `find` / `anchor` string must match **exactly once** in the target file — the applier rejects zero-match (hallucinated code) and multi-match (ambiguous edit) before they become silent bugs. The actual unified diff is generated from `git diff` after the ops are applied. See `agent/tools/edits.py`.

**Retrieval is keyword + symbol — no embeddings.** For focused issues, the issue text usually contains the relevant identifiers. Ripgrep over the workspace plus a lookup in the repo-map's symbol table gets us 80% of the value at 5% of the complexity of a vector store. (Graph-style *neighbor expansion* is sketched in the module docstring as a future signal but is not yet wired into the scorer.) See `agent/stages/retrieve.py` and [Retrieval &amp; ranking](#retrieval--ranking).

**Validation is the agent's correctness signal.** `gofmt → vet → build → test` runs after every Edit. On failure, the captured stderr is fed back to the Editor as `prior_error` on the next retry. Max 3 retries by default. See `agent/stages/validate.py` and the loop in `agent/loop.py`.

**Conventions are extracted, not hand-coded.** `agent/conventions.py` reads `CONTRIBUTING.md`, `.golangci.yml`, and the last 30 commit subjects to build a short string injected into the Planner / Editor / Summarizer prompts. That's how the agent picks up "lowercase imperative commit subjects" or "tests live next to source" without per-repo logic.

**Full tracing.** Every LLM call and every tool call writes a line to `runs/<run>/trace.jsonl`. This is the deliverable reviewers should read first.

**Prompt caching.** The system prompt (repo map + conventions) is wrapped in an `ephemeral` cache_control block. Across the Plan → Edit → Validate → Edit (retry) loop in one run, the system prompt repeats; caching drops cost ~10× on the second and subsequent calls.

**Provider-agnostic.** Adding OpenRouter or another OpenAI-compatible host = one branch in `build_llm()`. All stages talk to a single `LLM` Protocol, so swapping providers never touches stage code.

---

## Eval harness

```powershell
goagent eval evals\fixtures.yaml
```

Each fixture is a `(repo, issue#, accepted_PR#)` tuple. For each:
1. Run the agent end-to-end.
2. Score with three metrics:
   - **Files Jaccard** — overlap between agent-changed files and PR-changed files.
   - **Tests pass** — did the produced diff leave `go test ./...` green?
   - **LLM-as-judge** — a separate LLM call (`judge_with_llm` in `evals/scoring.py`, using the same provider abstraction as the agent — Gemini or Claude) rates root-cause similarity 1–5.

The shipped `evals/fixtures.yaml` has placeholders. Replace with real closed issues + their accepted PR numbers to actually score the agent.

---

## What this submission deliberately does NOT include

- **No vector DB / embeddings.** Reviewer-stated bias: simple beats complex. Keyword + symbol retrieval is sufficient for focused issues, and it's one file, not a service.
- **No multi-agent swarms.** One orchestrator, role-specialized prompts.
- **No automatic PR opening.** The assignment marks PR opening as optional. We produce the branch + patch + PR text and stop. A reviewer who wants to open the PR can do it with one `git push` + `gh pr create --body-file pr.md`.
- **No tool-use loop inside the Editor.** The Editor receives the full file content upfront and emits ops. A future improvement is symbol-aware chunking (send only the function bodies that match plan keywords) — useful for large files like cobra's `command.go` (~60KB).
- **No unit-test suite for the agent itself.** Correctness is demonstrated end-to-end instead: the checked-in [`samples/cobra_2376/`](samples/cobra_2376/) run and the [eval harness](#eval-harness). A `dev` extra (`pip install -e ".[dev]"`) ships `pytest`/`ruff` so a suite can be added; the `LLM` Protocol exists partly so tests can inject a fake provider.
- **No sandboxing of the target repo.** `go build`/`test` and `git reset --hard` run directly against the cloned workspace under `workspaces/`. Fine for the four vetted repos here; a hardening step (container/VM isolation) would be needed before pointing it at arbitrary repos.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `'go' not found on PATH` | Go installed but shell hasn't picked up PATH | Restart shell, OR (Windows): `$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")` |
| `<PROVIDER>_API_KEY is not set` | `.env` missing the key for the model you chose | Add the key to `.env`, OR switch model with `--model gemini-2.5-flash` (free) |
| `Request too large for model ... TPM limit` | Groq's per-minute token budget hit | Use a Groq model with higher TPM (Llama 4 Scout: 30k), OR switch provider |
| `socket connection closed unexpectedly` | Transient network drop OR your LLM account hit per-minute rate limit | Just retry. If repeated, wait 60s; if persistent, switch provider |
| Empty `changes.patch` after a run that showed "edit failed" 3 times | Each retry calls `git reset --hard HEAD` before attempting again, so failed-validation edits are wiped from the workspace | Read `trace.jsonl` `llm.call` events to see what Claude *tried* to write. Per-attempt patch saving is a planned improvement. |
| Edit hallucinates `find` strings that don't exist | `per_file_limit` in `agent/stages/edit.py` truncated the file the Editor needed to see | Bump `per_file_limit` (safe for Claude/Gemini; tight for Groq's TPM) |
| Gemini emits literal newlines inside JSON strings | Gemini's "thinking" mode interferes with structured output | Already disabled by default in `agent/llm.py:GeminiLLM` |
| A Claude run appears to hang for ~60s between stages | Deliberate `rate_limit.pause` to respect the 30k-input-tokens/min free-tier limit | Expected — see [Supported LLM providers](#supported-llm-providers). Use Gemini/Groq for no pause, or a paid Anthropic tier |

---


