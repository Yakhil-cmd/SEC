# Q1780: ledger: validate key format signature/domain

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `packages/icrc-ledger-types/src/icrc/metadata_key.rs`::validate_key_format with attacker-controlled Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this undercharge fees or mis-handle refunds when transfer/approval state changes across retries, violating the invariant that Rosetta construction/parse/submit must map one signed transaction to one ledger effect, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc/metadata_key.rs`::validate_key_format
- Entrypoint: publicly reachable validation path
- Attacker controls: Rosetta construction payloads, transaction identifiers, block ranges, ledger responses, and retry timing
- Exploit idea: undercharge fees or mis-handle refunds when transfer/approval state changes across retries
- Invariant to test: Rosetta construction/parse/submit must map one signed transaction to one ledger effect
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs; mutate domain separators, registry versions, signer IDs, and message bytes independently
