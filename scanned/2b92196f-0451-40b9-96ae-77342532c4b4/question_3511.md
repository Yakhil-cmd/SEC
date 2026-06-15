# Q3511: int In Slice consensus invariant edge 1ac5

## Question
Can an unprivileged attacker reach `intInSlice` in `sei-tendermint/internal/state/indexer/block/kv/util.go` via publicly submitted transaction or peer-delivered block/proposal data processed by validators, controlling transaction payloads, proposer-included ordering, block data size, evidence payloads, and peer-visible proposal inputs, and exploit encoding, evidence, or block metadata edge cases to panic or reject valid blocks on default validators so that the invariant `all honest validators must deterministically derive the same app state and block validity from the same proposal` fails, causing `Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages`?

## Target
- File/function: `sei-tendermint/internal/state/indexer/block/kv/util.go:14` `intInSlice`
- Entrypoint: publicly submitted transaction or peer-delivered block/proposal data processed by validators
- Attacker controls: transaction payloads, proposer-included ordering, block data size, evidence payloads, and peer-visible proposal inputs
- Exploit idea: exploit encoding, evidence, or block metadata edge cases to panic or reject valid blocks on default validators
- Invariant to test: all honest validators must deterministically derive the same app state and block validity from the same proposal
- Expected Immunefi impact: Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages
- Fast validation: Run a two-validator localnet or deterministic state test, feed the crafted tx/proposal/evidence, and compare app hash, block result, panic behavior, and processing time.
