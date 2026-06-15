# Q3490: is Legacy Receipt evm core invariant edge 0885

## Question
Can an unprivileged attacker reach `isLegacyReceipt` in `x/evm/keeper/receipt.go` via public EVM transaction through CheckTx, ProcessProposal, or DeliverTx, controlling EVM tx type, chain id, nonce, gas caps, access list, calldata, value, authorization list, and account association state, and bypass ante, fee, nonce, chain-id, association, or stateless validation so execution consumes resources or mutates state incorrectly so that the invariant `all validators must reject or accept the same public EVM transaction with the same state side effects` fails, causing `Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages`?

## Target
- File/function: `x/evm/keeper/receipt.go:129` `isLegacyReceipt`
- Entrypoint: public EVM transaction through CheckTx, ProcessProposal, or DeliverTx
- Attacker controls: EVM tx type, chain id, nonce, gas caps, access list, calldata, value, authorization list, and account association state
- Exploit idea: bypass ante, fee, nonce, chain-id, association, or stateless validation so execution consumes resources or mutates state incorrectly
- Invariant to test: all validators must reject or accept the same public EVM transaction with the same state side effects
- Expected Immunefi impact: Medium: Block production delay exceeding 2.5 seconds on realistic validator hardware, caused by crafted transactions or messages
- Fast validation: Run the tx through CheckTx, ProcessProposal, DeliverTx, and receipt query paths, then diff nonce, balance, gas, logs, receipts, and committed state.
