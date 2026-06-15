# Q0072: Mark Block Gossip Complete consensus invariant edge 2b95

## Question
Can an unprivileged attacker reach `MarkBlockGossipComplete` in `sei-tendermint/internal/consensus/metrics.go` via publicly submitted transaction or peer-delivered block/proposal data processed by validators, controlling transaction payloads, proposer-included ordering, block data size, evidence payloads, and peer-visible proposal inputs, and exploit encoding, evidence, or block metadata edge cases to panic or reject valid blocks on default validators so that the invariant `public block/proposal data must not delay production by more than the in-scope threshold on realistic hardware` fails, causing `High: Network not being able to confirm new transactions from total shutdown or consensus failure`?

## Target
- File/function: `sei-tendermint/internal/consensus/metrics.go:188` `MarkBlockGossipComplete`
- Entrypoint: publicly submitted transaction or peer-delivered block/proposal data processed by validators
- Attacker controls: transaction payloads, proposer-included ordering, block data size, evidence payloads, and peer-visible proposal inputs
- Exploit idea: exploit encoding, evidence, or block metadata edge cases to panic or reject valid blocks on default validators
- Invariant to test: public block/proposal data must not delay production by more than the in-scope threshold on realistic hardware
- Expected Immunefi impact: High: Network not being able to confirm new transactions from total shutdown or consensus failure
- Fast validation: Run a two-validator localnet or deterministic state test, feed the crafted tx/proposal/evidence, and compare app hash, block result, panic behavior, and processing time.
