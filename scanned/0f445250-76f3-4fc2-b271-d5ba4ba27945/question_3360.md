# Q3360: Create Proposal Block consensus invariant edge 11dd

## Question
Can an unprivileged attacker reach `CreateProposalBlock` in `sei-tendermint/internal/state/execution.go` via publicly submitted transaction or peer-delivered block/proposal data processed by validators, controlling transaction payloads, proposer-included ordering, block data size, evidence payloads, and peer-visible proposal inputs, and make honest validators accept different derived state from the same public transaction or proposal data so that the invariant `all honest validators must deterministically derive the same app state and block validity from the same proposal` fails, causing `Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages`?

## Target
- File/function: `sei-tendermint/internal/state/execution.go:101` `CreateProposalBlock`
- Entrypoint: publicly submitted transaction or peer-delivered block/proposal data processed by validators
- Attacker controls: transaction payloads, proposer-included ordering, block data size, evidence payloads, and peer-visible proposal inputs
- Exploit idea: make honest validators accept different derived state from the same public transaction or proposal data
- Invariant to test: all honest validators must deterministically derive the same app state and block validity from the same proposal
- Expected Immunefi impact: Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages
- Fast validation: Run a two-validator localnet or deterministic state test, feed the crafted tx/proposal/evidence, and compare app hash, block result, panic behavior, and processing time.
