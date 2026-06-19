# Q1802: ledger: Display replay/idempotency

## Question
Can an unprivileged attacker enter through an exchange/client sends Rosetta API requests with crafted operations, blocks, metadata, or signatures and drive `packages/icrc-ledger-types/src/icrc1/account.rs`::Display with attacker-controlled ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests, violating the invariant that archive and index views must not create spendable balance or hide finalized debits, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc1/account.rs`::Display
- Entrypoint: an exchange/client sends Rosetta API requests with crafted operations, blocks, metadata, or signatures
- Attacker controls: ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields
- Exploit idea: bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests
- Invariant to test: archive and index views must not create spendable balance or hide finalized debits
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
