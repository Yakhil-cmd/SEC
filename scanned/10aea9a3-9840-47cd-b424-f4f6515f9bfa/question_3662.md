# Q3662: Last Baked Height evm core invariant edge a6fc

## Question
Can an unprivileged attacker reach `LastBakedHeight` in `x/evm/keeper/trace_db.go` via public EVM transaction through CheckTx, ProcessProposal, or DeliverTx, controlling EVM tx type, chain id, nonce, gas caps, access list, calldata, value, authorization list, and account association state, and make EVM receipts, logs, balances, nonce, or gas accounting diverge from committed Cosmos state so that the invariant `all validators must reject or accept the same public EVM transaction with the same state side effects` fails, causing `Critical: Permanent freezing of user funds of USD $5,000 or more`?

## Target
- File/function: `x/evm/keeper/trace_db.go:155` `LastBakedHeight`
- Entrypoint: public EVM transaction through CheckTx, ProcessProposal, or DeliverTx
- Attacker controls: EVM tx type, chain id, nonce, gas caps, access list, calldata, value, authorization list, and account association state
- Exploit idea: make EVM receipts, logs, balances, nonce, or gas accounting diverge from committed Cosmos state
- Invariant to test: all validators must reject or accept the same public EVM transaction with the same state side effects
- Expected Immunefi impact: Critical: Permanent freezing of user funds of USD $5,000 or more
- Fast validation: Run the tx through CheckTx, ProcessProposal, DeliverTx, and receipt query paths, then diff nonce, balance, gas, logs, receipts, and committed state.
