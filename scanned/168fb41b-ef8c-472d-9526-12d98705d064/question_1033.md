# Q1033: new Hashed Tx mempool invariant edge 6e53

## Question
Can an unprivileged attacker reach `newHashedTx` in `sei-tendermint/internal/mempool/tx.go` via public transaction gossip, CheckTx, or mempool recheck flow, controlling transaction bytes, nonce/order, gas values, signatures, account sequence, message mix, and repeated submission timing, and race sequence, nonce, replacement, or recheck behavior to evict valid transactions or keep invalid transactions consuming mempool capacity so that the invariant `nonce/sequence/recheck logic must not let invalid txs crowd out valid txs or delay block production` fails, causing `Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds`?

## Target
- File/function: `sei-tendermint/internal/mempool/tx.go:38` `newHashedTx`
- Entrypoint: public transaction gossip, CheckTx, or mempool recheck flow
- Attacker controls: transaction bytes, nonce/order, gas values, signatures, account sequence, message mix, and repeated submission timing
- Exploit idea: race sequence, nonce, replacement, or recheck behavior to evict valid transactions or keep invalid transactions consuming mempool capacity
- Invariant to test: nonce/sequence/recheck logic must not let invalid txs crowd out valid txs or delay block production
- Expected Immunefi impact: Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds
- Fast validation: Submit crafted tx bytes through CheckTx, ProcessProposal, and DeliverTx with default config, then compare admission, rejection point, fees, nonce, and elapsed validation time.
