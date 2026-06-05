# goagent

An agentic AI contributor for open-source Go projects. Give it a GitHub issue number from one of four approved Go repos and it produces a code change, runs `go build`/`vet`/`test`, and writes a PR title and body.

This is a take-home assignment submission. The goal isn't the LLM — it's the **framework** around the LLM: a deterministic six-stage pipeline with a retry loop, structured edits (not raw diffs), and full per-run tracing.

```
Issue ──▶ Fetch ──▶ Index ──▶ Retrieve ──▶ Plan ──▶ Edit ──▶ Validate ──▶ Summarize ──▶ patch + pr.md
                                                       ▲           │
                                                       └── retry ──┘
```

---

## Table of contents

- [Quick start (5 minutes)](#quick-start-5-minutes)
- [Architecture](#architecture)
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

A linear six-stage pipeline. The only place stage order is defined is `agent/loop.py`. Each box is one Python module under `agent/stages/`.

```
              ┌──────────────────────────────────────────────────────────┐
              │                                                          ▼
  Issue ──▶ Fetch ──▶ Index ──▶ Retrieve ──▶ Plan ──▶ Edit ──▶ Validate ─┴──▶ Summarize ──▶ patch + pr.md
            (REST)    (FS walk  (ripgrep +   (LLM:    (LLM:    (gofmt,        (LLM:
                       + regex   symbol      hypoth., structured vet, build,   title +
                       AST       grep +      files,   edits)   test ./pkg/)   body)
                       cache)    neighbors)  tests)
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
3. **LLM-as-judge** — separate Claude call rates root-cause similarity 1–5.

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

**Read `trace.jsonl` first** — it's the agent's diary. One line per event with token counts, costs, and previews.

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

**Retrieval is keyword + symbol + neighbors — no embeddings.** For focused issues, the issue text usually contains the relevant identifiers. Ripgrep over the workspace plus a lookup in the repo-map's symbol table gets us 80% of the value at 5% of the complexity of a vector store. See `agent/stages/retrieve.py`.

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
   - **LLM-as-judge** — separate Claude call rates root-cause similarity 1–5.

The shipped `evals/fixtures.yaml` has placeholders. Replace with real closed issues + their accepted PR numbers to actually score the agent.

---

## What this submission deliberately does NOT include

- **No vector DB / embeddings.** Reviewer-stated bias: simple beats complex. Keyword + symbol retrieval is sufficient for focused issues, and it's one file, not a service.
- **No multi-agent swarms.** One orchestrator, role-specialized prompts.
- **No automatic PR opening.** The assignment marks PR opening as optional. We produce the branch + patch + PR text and stop. A reviewer who wants to open the PR can do it with one `git push` + `gh pr create --body-file pr.md`.
- **No tool-use loop inside the Editor.** The Editor receives the full file content upfront and emits ops. A future improvement is symbol-aware chunking (send only the function bodies that match plan keywords) — useful for large files like cobra's `command.go` (~60KB).

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
