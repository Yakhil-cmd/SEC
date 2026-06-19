# Q1776: ledger: as str rollback edge case

## Question
Can an unprivileged attacker enter through a caller queries archive/index state while ledger blocks are being appended or synchronized and drive `packages/icrc-ledger-types/src/icrc/metadata_key.rs`::as_str with attacker-controlled ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this undercharge fees or mis-handle refunds when transfer/approval state changes across retries, violating the invariant that Rosetta construction/parse/submit must map one signed transaction to one ledger effect, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc/metadata_key.rs`::as_str
- Entrypoint: a caller queries archive/index state while ledger blocks are being appended or synchronized
- Attacker controls: ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields
- Exploit idea: undercharge fees or mis-handle refunds when transfer/approval state changes across retries
- Invariant to test: Rosetta construction/parse/submit must map one signed transaction to one ledger effect
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs
