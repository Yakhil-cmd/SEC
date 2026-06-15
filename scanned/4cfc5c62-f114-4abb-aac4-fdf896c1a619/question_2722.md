# Q2722: Copy consensus invariant edge c35f

## Question
Can an unprivileged attacker reach `Copy` in `sei-tendermint/internal/consensus/types/peer_round_state.go` via publicly submitted transaction or peer-delivered block/proposal data processed by validators, controlling transaction payloads, proposer-included ordering, block data size, evidence payloads, and peer-visible proposal inputs, and force deterministic but excessive validation work during proposal processing or block execution so that the invariant `public block/proposal data must not delay production by more than the in-scope threshold on realistic hardware` fails, causing `High: Network not being able to confirm new transactions from total shutdown or consensus failure`?

## Target
- File/function: `sei-tendermint/internal/consensus/types/peer_round_state.go:52` `Copy`
- Entrypoint: publicly submitted transaction or peer-delivered block/proposal data processed by validators
- Attacker controls: transaction payloads, proposer-included ordering, block data size, evidence payloads, and peer-visible proposal inputs
- Exploit idea: force deterministic but excessive validation work during proposal processing or block execution
- Invariant to test: public block/proposal data must not delay production by more than the in-scope threshold on realistic hardware
- Expected Immunefi impact: High: Network not being able to confirm new transactions from total shutdown or consensus failure
- Fast validation: Run a two-validator localnet or deterministic state test, feed the crafted tx/proposal/evidence, and compare app hash, block result, panic behavior, and processing time.
