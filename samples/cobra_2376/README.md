# Sample run — cobra #2376

A recorded end-to-end run of the agent against
[spf13/cobra#2376](https://github.com/spf13/cobra/issues/2376):
*"bug: `ValidArgsFunction` receives partial input when argument contains colons in Bash"*.

Outcome: **success on the 2nd attempt** (validation passed: `gofmt`, `go vet`,
`go build ./...`, `go test ./...`).

## What's in this directory

| File             | What it is                                                |
| ---------------- | --------------------------------------------------------- |
| `issue.json`     | The GitHub issue body and metadata as fetched.            |
| `repo_map.json`  | The indexed repo map produced by the Index stage.         |
| `plan.json`      | The Planner's structured hypothesis + files to edit.      |
| `trace.jsonl`    | Every LLM call and tool invocation, one JSON event/line.  |
| `changes.patch`  | The unified diff that validation approved.                |
| `pr.md`          | The PR title + body the Summarize stage produced.         |

## How to read the trace

`trace.jsonl` is the primary artifact. Each line is a JSON object with `ts`,
`kind`, and `data`. Notable kinds:

- `run.start` / `run.end` — top-level run boundaries
- `fetch_issue.done`, `index_repo.done`, `retrieve.done`, `plan.done`
- `llm.call` — every LLM round-trip (tokens, cost, prompt/response previews)
- `validate.done` — pass/fail of gofmt/vet/build/test for one attempt
- `edit.failed` — when the editor's output couldn't be applied (parse or
  ambiguous anchor); the retry loop feeds the error back into the next call

## Attempt timeline for this run

1. **Plan** — identified the root cause: Bash's `COMP_WORDBREAKS` splits
   words on `:`, so `ValidArgsFunction` only receives the suffix.
2. **Edit attempt 1** — applied, but failed `go build` (the inserted bash
   helper conflicted with Go's `fmt.Sprintf` indexed-arg verbs in the
   template string).
3. **Edit attempt 2** — succeeded. The editor rewrote the patch using only
   the already-present `%[1]s` indexed-argument style, sidestepping the
   format-string conflict. All four validation gates passed.
4. **Summarize** — produced `pr.md` with the title, summary, and validation
   record.

## Reproducing

```powershell
$env:GEMINI_API_KEY = "..."        # or ANTHROPIC_API_KEY for Claude
goagent solve cobra 2376
```

Results will land in `runs/<timestamp>_cobra_2376/`. The numbers won't be
identical — LLM output is non-deterministic even at temperature 0 — but the
overall pipeline shape (the 6 stages, the retry loop, the artifacts) will
match what's recorded here.
