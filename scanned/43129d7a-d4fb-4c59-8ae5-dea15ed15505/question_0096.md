# Q96: chain fusion: next provider rollback edge case

## Question
Can an unprivileged attacker enter through a protocol peer or adapter client supplies chain-fusion payloads that enter consensus or minter state and drive `rs/bitcoin/checker/src/providers.rs`::next_provider with attacker-controlled deposit addresses, UTXO sets, withdrawal amounts, destination addresses, ERC20 logs, block heights, and RPC payloads to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this accept an external-chain log/UTXO/transaction under the wrong finality, address, token, or chain context, violating the invariant that threshold signing must only authorize transactions derived from valid minter state, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/checker/src/providers.rs`::next_provider
- Entrypoint: a protocol peer or adapter client supplies chain-fusion payloads that enter consensus or minter state
- Attacker controls: deposit addresses, UTXO sets, withdrawal amounts, destination addresses, ERC20 logs, block heights, and RPC payloads
- Exploit idea: accept an external-chain log/UTXO/transaction under the wrong finality, address, token, or chain context
- Invariant to test: threshold signing must only authorize transactions derived from valid minter state
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency
