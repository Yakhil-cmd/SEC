# Q1813: ledger: validate canonical encoding

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `packages/icrc-ledger-types/src/icrc122/schema.rs`::validate with attacker-controlled ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests, violating the invariant that duplicate/replay windows must reject repeated value movement under all encodings, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc122/schema.rs`::validate
- Entrypoint: publicly reachable validation path
- Attacker controls: ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields
- Exploit idea: bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests
- Invariant to test: duplicate/replay windows must reject repeated value movement under all encodings
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
