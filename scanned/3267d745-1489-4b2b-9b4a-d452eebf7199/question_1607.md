# Q1607: Is Tx Type Allowed evm core invariant edge 6be9

## Question
Can an unprivileged attacker reach `IsTxTypeAllowed` in `x/evm/ante/preprocess.go` via public EVM transaction through CheckTx, ProcessProposal, or DeliverTx, controlling EVM tx type, chain id, nonce, gas caps, access list, calldata, value, authorization list, and account association state, and trigger a revert/error path after partial fee, nonce, or balance updates and leave protocol accounting inconsistent so that the invariant `all validators must reject or accept the same public EVM transaction with the same state side effects` fails, causing `Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds`?

## Target
- File/function: `x/evm/ante/preprocess.go:207` `IsTxTypeAllowed`
- Entrypoint: public EVM transaction through CheckTx, ProcessProposal, or DeliverTx
- Attacker controls: EVM tx type, chain id, nonce, gas caps, access list, calldata, value, authorization list, and account association state
- Exploit idea: trigger a revert/error path after partial fee, nonce, or balance updates and leave protocol accounting inconsistent
- Invariant to test: all validators must reject or accept the same public EVM transaction with the same state side effects
- Expected Immunefi impact: Low: Manipulation of transaction fee calculation resulting in fees outside protocol-defined bounds
- Fast validation: Run the tx through CheckTx, ProcessProposal, DeliverTx, and receipt query paths, then diff nonce, balance, gas, logs, receipts, and committed state.
