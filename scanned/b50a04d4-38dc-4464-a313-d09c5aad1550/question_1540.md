# Q1540: types packages: source signature/domain

## Question
Can an unprivileged attacker enter through a Rosetta/ICRC/management-canister caller supplies edge-case type values that flow into production logic and drive `packages/ic-error-types/src/lib.rs`::source with attacker-controlled encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this overflow, truncate, or normalize security-critical fields before authorization or accounting checks, violating the invariant that numeric/account/principal conversions must not truncate, alias, or bypass authorization/accounting checks, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/ic-error-types/src/lib.rs`::source
- Entrypoint: a Rosetta/ICRC/management-canister caller supplies edge-case type values that flow into production logic
- Attacker controls: encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures
- Exploit idea: overflow, truncate, or normalize security-critical fields before authorization or accounting checks
- Invariant to test: numeric/account/principal conversions must not truncate, alias, or bypass authorization/accounting checks
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; mutate domain separators, registry versions, signer IDs, and message bytes independently
