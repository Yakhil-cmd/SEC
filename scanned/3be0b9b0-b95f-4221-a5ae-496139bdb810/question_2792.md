# Q2792: chain fusion: get height replay/idempotency

## Question
Can an unprivileged attacker enter through a protocol peer or adapter client supplies chain-fusion payloads that enter consensus or minter state and drive `rs/bitcoin/validation/src/header.rs`::get_height with attacker-controlled minter state transitions, retry ordering, chain-key signature requests, and external-chain transaction encodings to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this accept an external-chain log/UTXO/transaction under the wrong finality, address, token, or chain context, violating the invariant that threshold signing must only authorize transactions derived from valid minter state, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/validation/src/header.rs`::get_height
- Entrypoint: a protocol peer or adapter client supplies chain-fusion payloads that enter consensus or minter state
- Attacker controls: minter state transitions, retry ordering, chain-key signature requests, and external-chain transaction encodings
- Exploit idea: accept an external-chain log/UTXO/transaction under the wrong finality, address, token, or chain context
- Invariant to test: threshold signing must only authorize transactions derived from valid minter state
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
