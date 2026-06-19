# Q20: types packages: decode signature/domain

## Question
Can an unprivileged attacker enter through a Rosetta/ICRC/management-canister caller supplies edge-case type values that flow into production logic and drive `packages/icrc-cbor/src/nat.rs`::decode with attacker-controlled large integers, malformed lengths, duplicated fields, unknown variants, and cross-format conversions to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this make two encodings decode to semantically different values across components using the same shared type, violating the invariant that numeric/account/principal conversions must not truncate, alias, or bypass authorization/accounting checks, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/icrc-cbor/src/nat.rs`::decode
- Entrypoint: a Rosetta/ICRC/management-canister caller supplies edge-case type values that flow into production logic
- Attacker controls: large integers, malformed lengths, duplicated fields, unknown variants, and cross-format conversions
- Exploit idea: make two encodings decode to semantically different values across components using the same shared type
- Invariant to test: numeric/account/principal conversions must not truncate, alias, or bypass authorization/accounting checks
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; mutate domain separators, registry versions, signer IDs, and message bytes independently
