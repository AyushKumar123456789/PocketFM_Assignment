# goagent — an agentic AI contributor for open-source Go projects

`goagent` is a take-home assignment submission. It builds a small, deterministic
**framework** around an LLM that can take a GitHub issue from one of four
approved Go repositories, find the relevant files, propose a code change,
run validation (`go build` / `vet` / `test` / `gofmt`), and produce a PR
title + body.

The point of this submission is the framework, not the LLM. The agent's
behavior is shaped by a six-stage pipeline with a tight retry loop between
the Editor and the Validator — not by a single mega-prompt.

## Supported repos

| Config name      | Repo                                      |
|------------------|-------------------------------------------|
| `cobra`          | spf13/cobra                               |
| `gin`            | gin-gonic/gin                             |
| `validator`      | go-playground/validator                   |
| `golangci-lint`  | golangci/golangci-lint                    |

Adding a fifth repo is one new `configs/<name>.yaml` file.

## Setup

Prerequisites (all checked at startup; the agent fails fast with a clear
message if any are missing):

- **Python 3.10+**
- **Go 1.21+** — install: https://go.dev/doc/install
- **git**
- **An LLM API key — one of:**
  - **Gemini** (recommended, has a free tier): https://aistudio.google.com/apikey — no credit card. Default model.
  - **Anthropic Claude** (paid): https://console.anthropic.com/

The default model is `gemini-2.5-flash` (free tier: 15 RPM, 1500 req/day,
1M context window). To use Claude instead: `--model claude-sonnet-4-6`
and set `ANTHROPIC_API_KEY`.

Optional but recommended:
- **ripgrep** (`rg`) — speeds up retrieval. If not installed, a pure-Python
  fallback is used.
- **GitHub token** (for higher API rate limits when fetching issues).

```powershell
git clone https://github.com/<you>/goagent.git
cd goagent
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # bash: source .venv/bin/activate
pip install -e .

cp .env.example .env             # then open .env and paste in your key
goagent --help                   # sanity check — should print usage
goagent repos                    # confirms config files load
```

The minimum `.env` for a free run is one line:

```
GEMINI_API_KEY=<your-key-from-aistudio.google.com/apikey>
```

## Run

List the four supported repos:

```powershell
goagent repos
# cobra          — https://github.com/spf13/cobra.git
# gin            — https://github.com/gin-gonic/gin.git
# validator      — https://github.com/go-playground/validator.git
# golangci-lint  — https://github.com/golangci/golangci-lint.git
```

Solve one issue (use any of the four config names):

```powershell
goagent solve cobra 2376         # repo config name, issue number
goagent solve gin 4003
goagent solve validator 1289
goagent solve golangci-lint 5142
```

The first run on a given repo clones it into `workspaces/<repo>/` (a few
seconds to a few minutes depending on repo size). Later runs reuse that
clone.

`--dry-run` stops after the Plan stage (cheap; lets you eyeball the plan
before committing to Edit + Validate).

`--model` overrides the LLM:
- `gemini-2.5-flash` (default, free tier)
- `gemini-2.5-pro` (free tier, slower, higher quality)
- `claude-sonnet-4-6` (paid)
- `claude-opus-4-7` (paid, highest quality)

The provider is inferred from the model-name prefix; you only need the key
for the provider you're actually using.

Each run writes its artifacts under `runs/<timestamp>_<repo>_<issue>/`:

```
runs/20260604T142301Z_cobra_2178/
├── issue.json          # fetched GitHub issue + comments
├── repo_map.json       # indexed package tree + symbols
├── plan.json           # Planner output (hypothesis, files, tests, risks)
├── trace.jsonl         # every LLM call + tool call, append-only
├── changes.patch       # the produced unified diff
└── pr.md               # PR title + body
```

The patch can be applied with `git apply` inside `workspaces/<repo>/`.

## Eval harness

```powershell
goagent eval evals\fixtures.yaml
```

Fixtures are `(repo, issue, accepted_PR)` tuples. For each fixture, the
agent runs end-to-end, then the harness scores it:

1. **Files Jaccard** — overlap between agent-changed files and PR-changed files.
2. **Tests pass** — did the change leave `go test ./...` green?
3. **LLM-as-judge** — a separate Claude call rates root-cause similarity 1–5.

`evals/fixtures.yaml` ships with placeholders — fill in real closed
issues to actually score the agent.

## Architecture

```
              ┌──────────────────────────────────────────────────────────┐
              │                                                          ▼
  Issue ──▶ Fetch ──▶ Index ──▶ Retrieve ──▶ Plan ──▶ Edit ──▶ Validate ─┴──▶ Summarize ──▶ patch + pr.md
            (REST)    (FS walk  (ripgrep +   (LLM:    (LLM:    (gofmt,        (LLM:
                       + regex   symbol      hypoth., structured vet, build,   title +
                       AST       grep +      files,   edits)   test ./pkg/)   body)
                       cache)    neighbors)  tests)
```

Every box is a module in `agent/`. The orchestrator (`agent/loop.py`) is
the only place stage order is defined.

### Key design decisions

**Structured edits, not raw diffs.** The Editor LLM emits a JSON list of
operations (`replace`, `insert_after`, `create_file`), not a unified diff.
Each `find`/`anchor` must match **exactly once** in the target file — the
applier rejects zero-match (hallucinated code) and multi-match (ambiguous
edit) cases before they become bugs. The actual diff is generated from
git after the ops are applied. See `agent/tools/edits.py`.

**Retrieval is keyword + symbol + neighbors, no embeddings.** For focused
issues, the issue text typically contains the relevant identifiers.
Ripgrep over the workspace plus a lookup in the repo-map's symbol table
gets us 80% of the value at 5% of the complexity of a vector store. See
`agent/stages/retrieve.py`.

**Validation is the agent's correctness signal.** `gofmt → vet → build →
test` runs after every Edit. On failure, the captured stderr is fed back
into the Editor with a "fix this" prompt. Max 3 retries; after that the
run exits cleanly with a "needs human review" note. See
`agent/stages/validate.py` and the loop in `agent/loop.py`.

**Conventions are extracted, not hand-coded.** `agent/conventions.py`
reads `CONTRIBUTING.md`, `.golangci.yml`, and the last 30 commit subjects
to build a short conventions string injected into the Planner, Editor,
and Summarizer prompts. This is how the agent picks up "lowercase
imperative commit subjects" or "tests live next to source" without our
having to encode those rules per repo.

**Full tracing.** Every LLM call and every tool call writes a line to
`runs/<run>/trace.jsonl`. This is what reviewers should read first to
understand how the agent reasoned.

**Prompt caching.** The system prompt (repo map + conventions) is wrapped
in an `ephemeral` cache_control block. Across the Plan → Edit →
Validate → Edit (retry) loop in one run, the system prompt is repeated
many times; caching drops the cost ~10x on the second and subsequent
calls.

## Repo layout

```
.
├── agent/                Pipeline modules
│   ├── cli.py            argparse entrypoint
│   ├── loop.py           orchestrator (the only stage-order definition)
│   ├── llm.py            Anthropic wrapper, prompt-caching, cost tracking
│   ├── tracing.py        append-only JSONL trace
│   ├── prompt_loader.py  loads prompts/*.md
│   ├── conventions.py    extracts CONTRIBUTING.md / .golangci.yml / commit style
│   ├── config.py         per-repo YAML loader
│   ├── stages/           fetch_issue, index_repo, retrieve, plan, edit, validate, summarize
│   └── tools/            fs, search (rg wrapper), edits (op applier), shell, workspace, symbols
├── configs/              one YAML per supported repo
├── prompts/              plan.md / edit.md / summarize.md
├── workspaces/           cloned target repos live here (gitignored)
├── runs/                 per-run artifacts (gitignored)
├── samples/              pre-recorded sample runs (checked in)
└── evals/                eval harness + fixtures
```

## What this submission deliberately does NOT include

- **No vector DB / embeddings.** Reviewer-stated bias: simple beats complex.
  Keyword + symbol retrieval is sufficient for focused issues and is
  one file, not a service.
- **No multi-agent swarms.** One orchestrator, role-specialized prompts.
- **No automatic PR opening.** The assignment marks PR opening as
  optional; we generate the branch + patch + PR text and stop. A
  reviewer who wants to actually open the PR can do it with one
  `git push` and `gh pr create --body-file pr.md`.

## Status of this checkin

Every module has its real interface, every non-LLM stage is fully
implemented (fetch, index, retrieve, validate, edits, shell, tracing).
The three LLM-bound stages (plan, edit, summarize) are live; an
end-to-end run on a real closed cobra issue is checked in at
`samples/cobra_2376/`.

Next steps before submission:
1. Add 1–2 more sample runs against other supported repos.
2. Fill in real fixtures in `evals/fixtures.yaml`.
