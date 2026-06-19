# Q1782: ledger: Metadata Key replay/idempotency

## Question
Can an unprivileged attacker enter through an exchange/client sends Rosetta API requests with crafted operations, blocks, metadata, or signatures and drive `packages/icrc-ledger-types/src/icrc/metadata_key.rs`::MetadataKey with attacker-controlled Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests, violating the invariant that archive and index views must not create spendable balance or hide finalized debits, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc/metadata_key.rs`::MetadataKey
- Entrypoint: an exchange/client sends Rosetta API requests with crafted operations, blocks, metadata, or signatures
- Attacker controls: Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing
- Exploit idea: bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests
- Invariant to test: archive and index views must not create spendable balance or hide finalized debits
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
