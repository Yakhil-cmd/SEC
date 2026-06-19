# Q1512: types packages: From replay/idempotency

## Question
Can an unprivileged attacker enter through a Rosetta/ICRC/management-canister caller supplies edge-case type values that flow into production logic and drive `packages/canlog/src/types/mod.rs`::From with attacker-controlled encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this accept a boundary type value that downstream consensus/ledger/governance logic assumes impossible, violating the invariant that numeric/account/principal conversions must not truncate, alias, or bypass authorization/accounting checks, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/canlog/src/types/mod.rs`::From
- Entrypoint: a Rosetta/ICRC/management-canister caller supplies edge-case type values that flow into production logic
- Attacker controls: encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures
- Exploit idea: accept a boundary type value that downstream consensus/ledger/governance logic assumes impossible
- Invariant to test: numeric/account/principal conversions must not truncate, alias, or bypass authorization/accounting checks
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
