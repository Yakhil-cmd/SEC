# Q2278: chain fusion: prune headers below height bounds/overflow

## Question
Can an unprivileged attacker enter through a canister requests Bitcoin/Ethereum/Dogecoin integration data through public management or adapter paths and drive `rs/bitcoin/adapter/src/header_cache.rs`::prune_headers_below_height with attacker-controlled minter state transitions, retry ordering, chain-key signature requests, and external-chain transaction encodings to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this obtain a chain-key signature for a transaction not authorized by the finalized minter state, violating the invariant that minter operations must be idempotent across retries, reimbursements, and adapter/RPC inconsistencies, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/adapter/src/header_cache.rs`::prune_headers_below_height
- Entrypoint: a canister requests Bitcoin/Ethereum/Dogecoin integration data through public management or adapter paths
- Attacker controls: minter state transitions, retry ordering, chain-key signature requests, and external-chain transaction encodings
- Exploit idea: obtain a chain-key signature for a transaction not authorized by the finalized minter state
- Invariant to test: minter operations must be idempotent across retries, reimbursements, and adapter/RPC inconsistencies
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
