# Q1292: ledger: into response replay/idempotency

## Question
Can an unprivileged attacker enter through a caller queries archive/index state while ledger blocks are being appended or synchronized and drive `rs/rosetta-api/icrc1/src/common/types.rs`::into_response with attacker-controlled ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this confuse account/subaccount encoding so authorization or accounting applies to the wrong account, violating the invariant that Rosetta construction/parse/submit must map one signed transaction to one ledger effect, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `rs/rosetta-api/icrc1/src/common/types.rs`::into_response
- Entrypoint: a caller queries archive/index state while ledger blocks are being appended or synchronized
- Attacker controls: ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields
- Exploit idea: confuse account/subaccount encoding so authorization or accounting applies to the wrong account
- Invariant to test: Rosetta construction/parse/submit must map one signed transaction to one ledger effect
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
