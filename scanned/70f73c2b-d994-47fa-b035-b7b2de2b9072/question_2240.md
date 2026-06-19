# Q2240: chain fusion: discard signature/domain

## Question
Can an unprivileged attacker enter through a protocol peer or adapter client supplies chain-fusion payloads that enter consensus or minter state and drive `rs/bitcoin/adapter/src/connection.rs`::discard with attacker-controlled minter state transitions, retry ordering, chain-key signature requests, and external-chain transaction encodings to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this obtain a chain-key signature for a transaction not authorized by the finalized minter state, violating the invariant that threshold signing must only authorize transactions derived from valid minter state, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/adapter/src/connection.rs`::discard
- Entrypoint: a protocol peer or adapter client supplies chain-fusion payloads that enter consensus or minter state
- Attacker controls: minter state transitions, retry ordering, chain-key signature requests, and external-chain transaction encodings
- Exploit idea: obtain a chain-key signature for a transaction not authorized by the finalized minter state
- Invariant to test: threshold signing must only authorize transactions derived from valid minter state
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency; mutate domain separators, registry versions, signer IDs, and message bytes independently
