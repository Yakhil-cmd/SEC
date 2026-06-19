# Q1536: types packages: is system error rollback edge case

## Question
Can an unprivileged attacker enter through a Rosetta/ICRC/management-canister caller supplies edge-case type values that flow into production logic and drive `packages/ic-error-types/src/lib.rs`::is_system_error with attacker-controlled serialization round trips, canonical ordering, domain separators, and request identifiers to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this overflow, truncate, or normalize security-critical fields before authorization or accounting checks, violating the invariant that numeric/account/principal conversions must not truncate, alias, or bypass authorization/accounting checks, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/ic-error-types/src/lib.rs`::is_system_error
- Entrypoint: a Rosetta/ICRC/management-canister caller supplies edge-case type values that flow into production logic
- Attacker controls: serialization round trips, canonical ordering, domain separators, and request identifiers
- Exploit idea: overflow, truncate, or normalize security-critical fields before authorization or accounting checks
- Invariant to test: numeric/account/principal conversions must not truncate, alias, or bypass authorization/accounting checks
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing
