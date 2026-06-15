# Q3056: Set Valid Block consensus invariant edge 2cea

## Question
Can an unprivileged attacker reach `SetValidBlock` in `sei-tendermint/internal/consensus/types/round_state.go` via publicly submitted transaction or peer-delivered block/proposal data processed by validators, controlling transaction payloads, proposer-included ordering, block data size, evidence payloads, and peer-visible proposal inputs, and force deterministic but excessive validation work during proposal processing or block execution so that the invariant `public block/proposal data must not delay production by more than the in-scope threshold on realistic hardware` fails, causing `Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages`?

## Target
- File/function: `sei-tendermint/internal/consensus/types/round_state.go:308` `SetValidBlock`
- Entrypoint: publicly submitted transaction or peer-delivered block/proposal data processed by validators
- Attacker controls: transaction payloads, proposer-included ordering, block data size, evidence payloads, and peer-visible proposal inputs
- Exploit idea: force deterministic but excessive validation work during proposal processing or block execution
- Invariant to test: public block/proposal data must not delay production by more than the in-scope threshold on realistic hardware
- Expected Immunefi impact: Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages
- Fast validation: Run a two-validator localnet or deterministic state test, feed the crafted tx/proposal/evidence, and compare app hash, block result, panic behavior, and processing time.
