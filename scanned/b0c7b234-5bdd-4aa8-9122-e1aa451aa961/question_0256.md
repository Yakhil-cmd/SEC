# Q0256: End Block app tx invariant edge 56c6

## Question
Can an unprivileged attacker reach `EndBlock` in `app/legacyabci/end_block.go` via public Cosmos/EVM transaction processed by BaseApp and ante handlers, controlling transaction bytes, signatures, fee fields, message ordering, extension options, gas limits, and account sequence values, and make transaction decoding, ante handling, fee charging, or message execution disagree about the same tx bytes so that the invariant `public transaction processing must not panic, undercharge fees, or commit partial state on failure paths` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `app/legacyabci/end_block.go:22` `EndBlock`
- Entrypoint: public Cosmos/EVM transaction processed by BaseApp and ante handlers
- Attacker controls: transaction bytes, signatures, fee fields, message ordering, extension options, gas limits, and account sequence values
- Exploit idea: make transaction decoding, ante handling, fee charging, or message execution disagree about the same tx bytes
- Invariant to test: public transaction processing must not panic, undercharge fees, or commit partial state on failure paths
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Encode the transaction through the app TxConfig, execute CheckTx and FinalizeBlock/DeliverTx, and assert fees, gas, sequence, and state changes match the same interpretation.
