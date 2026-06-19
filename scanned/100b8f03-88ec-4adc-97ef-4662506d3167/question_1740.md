# Q1740: ledger: From signature/domain

## Question
Can an unprivileged attacker enter through a caller queries archive/index state while ledger blocks are being appended or synchronized and drive `packages/icrc-ledger-types/src/icrc/generic_metadata_value.rs`::From with attacker-controlled accounts, subaccounts, spender, amount, fee, memo, created_at_time, block index, operation order, and signatures to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests, violating the invariant that Rosetta construction/parse/submit must map one signed transaction to one ledger effect, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc/generic_metadata_value.rs`::From
- Entrypoint: a caller queries archive/index state while ledger blocks are being appended or synchronized
- Attacker controls: accounts, subaccounts, spender, amount, fee, memo, created_at_time, block index, operation order, and signatures
- Exploit idea: bypass duplicate detection or allowance decrementing by racing replayed but differently encoded requests
- Invariant to test: Rosetta construction/parse/submit must map one signed transaction to one ledger effect
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs; mutate domain separators, registry versions, signer IDs, and message bytes independently
