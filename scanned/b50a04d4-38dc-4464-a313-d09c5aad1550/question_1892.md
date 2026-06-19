# Q1892: ledger: Approve replay/idempotency

## Question
Can an unprivileged attacker enter through public approval/allowance flow and drive `packages/icrc-ledger-types/src/icrc3/transactions.rs`::Approve with attacker-controlled ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this confuse account/subaccount encoding so authorization or accounting applies to the wrong account, violating the invariant that Rosetta construction/parse/submit must map one signed transaction to one ledger effect, and produce HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure?

## Target
- File/function: `packages/icrc-ledger-types/src/icrc3/transactions.rs`::Approve
- Entrypoint: public approval/allowance flow
- Attacker controls: ICRC metadata, allowance expirations, archive callbacks, index sync heights, and duplicate detection fields
- Exploit idea: confuse account/subaccount encoding so authorization or accounting applies to the wrong account
- Invariant to test: Rosetta construction/parse/submit must map one signed transaction to one ledger effect
- Expected HackenProof impact: HackenProof High/Critical: theft, illegal minting, fund freezing, or exchange-facing ledger integrity failure
- Fast validation: run ledger/Rosetta state-machine tests with replayed operations and assert balance conservation and one-to-one transaction IDs; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
