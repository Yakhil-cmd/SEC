# Q2403: chain fusion: new and validate canonical encoding

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/bitcoin/checker/src/state.rs`::new_and_validate with attacker-controlled deposit addresses, UTXO sets, withdrawal amounts, destination addresses, ERC20 logs, block heights, and RPC payloads to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this cause minter and ledger state to diverge after a failed withdrawal, reimbursement, or callback retry, violating the invariant that external-chain evidence must be bound to chain, token, address, finality, and ledger account context, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/checker/src/state.rs`::new_and_validate
- Entrypoint: publicly reachable validation path
- Attacker controls: deposit addresses, UTXO sets, withdrawal amounts, destination addresses, ERC20 logs, block heights, and RPC payloads
- Exploit idea: cause minter and ledger state to diverge after a failed withdrawal, reimbursement, or callback retry
- Invariant to test: external-chain evidence must be bound to chain, token, address, finality, and ledger account context
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
