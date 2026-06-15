# Q2473: Set Custom Precompiles evm core invariant edge 3c7e

## Question
Can an unprivileged attacker reach `SetCustomPrecompiles` in `x/evm/keeper/keeper.go` via public EVM transaction through CheckTx, ProcessProposal, or DeliverTx, controlling EVM tx type, chain id, nonce, gas caps, access list, calldata, value, authorization list, and account association state, and make EVM receipts, logs, balances, nonce, or gas accounting diverge from committed Cosmos state so that the invariant `EVM ante, execution, receipts, nonce, balances, gas purchase/refund, and Cosmos state commits must be mutually consistent` fails, causing `Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield`?

## Target
- File/function: `x/evm/keeper/keeper.go:161` `SetCustomPrecompiles`
- Entrypoint: public EVM transaction through CheckTx, ProcessProposal, or DeliverTx
- Attacker controls: EVM tx type, chain id, nonce, gas caps, access list, calldata, value, authorization list, and account association state
- Exploit idea: make EVM receipts, logs, balances, nonce, or gas accounting diverge from committed Cosmos state
- Invariant to test: EVM ante, execution, receipts, nonce, balances, gas purchase/refund, and Cosmos state commits must be mutually consistent
- Expected Immunefi impact: Critical: Direct loss of user funds of USD $5,000 or more, excluding gas fees and yield
- Fast validation: Run the tx through CheckTx, ProcessProposal, DeliverTx, and receipt query paths, then diff nonce, balance, gas, logs, receipts, and committed state.
