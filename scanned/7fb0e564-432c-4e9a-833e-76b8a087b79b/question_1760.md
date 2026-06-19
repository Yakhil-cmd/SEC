# Q1760: ledger: is text signature/domain

## Question
Can an unprivileged attacker enter through a caller queries archive/index state while ledger blocks are being appended or synchronized and drive `packages/icrc-ledger-types/src/icrc/generic_value_predicate.rs`::is_text with attacker-controlled ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this make ledger, archive, index, or Rosetta derive different balances or transaction IDs for one operation, violating the invariant that Rosetta construction/parse/submit must map one signed transaction to one ledger effect, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc/generic_value_predicate.rs`::is_text
- Entrypoint: a caller queries archive/index state while ledger blocks are being appended or synchronized
- Attacker controls: ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields
- Exploit idea: make ledger, archive, index, or Rosetta derive different balances or transaction IDs for one operation
- Invariant to test: Rosetta construction/parse/submit must map one signed transaction to one ledger effect
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs; mutate domain separators, registry versions, signer IDs, and message bytes independently
