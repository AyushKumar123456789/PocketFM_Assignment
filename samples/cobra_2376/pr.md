# fix: reassemble colon-split words in bash completion scripts

## Summary
- Bash splits on `:` (a COMP_WORDBREAKS character), causing `ValidArgsFunction` to receive only the partial word after the last colon
- Add colon-word reassembly logic to both V1 (`bash_completions.go`) and V2 (`bash_completionsV2.go`) generated scripts so `cur` and `words` reflect the full unsplit token before invoking `__complete`
- Pattern mirrors the well-known `_get_comp_words_by_ref` approach used by other completion frameworks

## Closes
Closes #2376

## Validation
- gofmt: ok
- go vet: ok
- go build ./...: ok
- go test ./affected/...: ok

## Notes
- The Go `fmt.Sprintf` format strings in the bash template required care to avoid treating bash `:` as a format verb; the fix uses the existing `%[1]s` indexed-argument pattern to sidestep conflicts
- Both V1 and V2 scripts are updated for consistency, though V2 is the active default
