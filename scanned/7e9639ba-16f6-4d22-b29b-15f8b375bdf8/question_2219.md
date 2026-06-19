# Q2219: chain fusion: address limits certification/witness

## Question
Can an unprivileged attacker enter through a caller controls UTXOs, Ethereum logs, RPC responses, transaction IDs, memos, fees, and retry timing and drive `rs/bitcoin/adapter/src/config.rs`::address_limits with attacker-controlled deposit addresses, UTXO sets, withdrawal amounts, destination addresses, ERC20 logs, block heights, and RPC payloads to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this accept an external-chain log/UTXO/transaction under the wrong finality, address, token, or chain context, violating the invariant that external-chain evidence must be bound to chain, token, address, finality, and ledger account context, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/adapter/src/config.rs`::address_limits
- Entrypoint: a caller controls UTXOs, Ethereum logs, RPC responses, transaction IDs, memos, fees, and retry timing
- Attacker controls: deposit addresses, UTXO sets, withdrawal amounts, destination addresses, ERC20 logs, block heights, and RPC payloads
- Exploit idea: accept an external-chain log/UTXO/transaction under the wrong finality, address, token, or chain context
- Invariant to test: external-chain evidence must be bound to chain, token, address, finality, and ledger account context
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency
