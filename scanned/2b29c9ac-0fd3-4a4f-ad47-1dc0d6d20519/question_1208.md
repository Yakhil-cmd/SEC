# Q1208: ledger: principal id from public key bounds/overflow

## Question
Can an unprivileged attacker enter through a caller queries archive/index state while ledger blocks are being appended or synchronized and drive `rs/rosetta-api/common/rosetta_core/src/convert.rs`::principal_id_from_public_key with attacker-controlled ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests, violating the invariant that Rosetta construction/parse/submit must map one signed transaction to one ledger effect, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `rs/rosetta-api/common/rosetta_core/src/convert.rs`::principal_id_from_public_key
- Entrypoint: a caller queries archive/index state while ledger blocks are being appended or synchronized
- Attacker controls: ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields
- Exploit idea: bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests
- Invariant to test: Rosetta construction/parse/submit must map one signed transaction to one ledger effect
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
