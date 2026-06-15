# Q0956: Get Events Of Type app tx invariant edge 3202

## Question
Can an unprivileged attacker reach `GetEventsOfType` in `app/receipt.go` via public Cosmos/EVM transaction processed by BaseApp and ante handlers, controlling transaction bytes, signatures, fee fields, message ordering, extension options, gas limits, and account sequence values, and make transaction decoding, ante handling, fee charging, or message execution disagree about the same tx bytes so that the invariant `public transaction processing must not panic, undercharge fees, or commit partial state on failure paths` fails, causing `Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages`?

## Target
- File/function: `app/receipt.go:403` `GetEventsOfType`
- Entrypoint: public Cosmos/EVM transaction processed by BaseApp and ante handlers
- Attacker controls: transaction bytes, signatures, fee fields, message ordering, extension options, gas limits, and account sequence values
- Exploit idea: make transaction decoding, ante handling, fee charging, or message execution disagree about the same tx bytes
- Invariant to test: public transaction processing must not panic, undercharge fees, or commit partial state on failure paths
- Expected Immunefi impact: Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages
- Fast validation: Encode the transaction through the app TxConfig, execute CheckTx and FinalizeBlock/DeliverTx, and assert fees, gas, sequence, and state changes match the same interpretation.
