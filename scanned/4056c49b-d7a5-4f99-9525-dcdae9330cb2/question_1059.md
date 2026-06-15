# Q1059: New Query All Evidence Request evidence invariant edge 31ae

## Question
Can an unprivileged attacker reach `NewQueryAllEvidenceRequest` in `sei-cosmos/x/evidence/types/querier.go` via public evidence submission or validator evidence processing flow, controlling evidence bytes, validator identities, heights, timestamps, duplicate vote fields, and replay timing, and submit crafted evidence that is accepted as valid but maps to the wrong validator, height, or voting power so that the invariant `evidence replay or malformed fields must not cause wrong slashing, chain halt, or excessive validation work` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `sei-cosmos/x/evidence/types/querier.go:21` `NewQueryAllEvidenceRequest`
- Entrypoint: public evidence submission or validator evidence processing flow
- Attacker controls: evidence bytes, validator identities, heights, timestamps, duplicate vote fields, and replay timing
- Exploit idea: submit crafted evidence that is accepted as valid but maps to the wrong validator, height, or voting power
- Invariant to test: evidence replay or malformed fields must not cause wrong slashing, chain halt, or excessive validation work
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Submit duplicate/malformed evidence in a deterministic app test and assert validator mapping, slashing, app hash, and processing time are consistent.
