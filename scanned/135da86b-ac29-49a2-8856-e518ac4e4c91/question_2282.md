# Q2282: chain fusion: tx get header replay/idempotency

## Question
Can an unprivileged attacker enter through a canister requests Bitcoin/Ethereum/Dogecoin integration data through public management or adapter paths and drive `rs/bitcoin/adapter/src/header_cache.rs`::tx_get_header with attacker-controlled minter state transitions, retry ordering, chain-key signature requests, and external-chain transaction encodings to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this obtain a chain-key signature for a transaction not authorized by the finalized minter state, violating the invariant that minter operations must be idempotent across retries, reimbursements, and adapter/RPC inconsistencies, and produce HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss?

## Target
- File/function: `rs/bitcoin/adapter/src/header_cache.rs`::tx_get_header
- Entrypoint: a canister requests Bitcoin/Ethereum/Dogecoin integration data through public management or adapter paths
- Attacker controls: minter state transitions, retry ordering, chain-key signature requests, and external-chain transaction encodings
- Exploit idea: obtain a chain-key signature for a transaction not authorized by the finalized minter state
- Invariant to test: minter operations must be idempotent across retries, reimbursements, and adapter/RPC inconsistencies
- Expected HackenProof impact: HackenProof Critical/High: chain-fusion asset theft, illegal ck-token minting, or permanent withdrawal/deposit loss
- Fast validation: simulate external-chain events and ledger callbacks in state-machine tests, then assert supply conservation and idempotency; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
