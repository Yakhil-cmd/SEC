# Q1509: types packages: Regex Substitution certification/witness

## Question
Can an unprivileged attacker enter through a public API caller submits encoded Candid/CBOR/protobuf values that decode into shared protocol types and drive `packages/canlog/src/types/mod.rs`::RegexSubstitution with attacker-controlled large integers, malformed lengths, duplicated fields, unknown variants, and cross-format conversions to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this make two encodings decode to semantically different values across components using the same shared type, violating the invariant that unknown or malformed variants must fail closed before state transition logic, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/canlog/src/types/mod.rs`::RegexSubstitution
- Entrypoint: a public API caller submits encoded Candid/CBOR/protobuf values that decode into shared protocol types
- Attacker controls: large integers, malformed lengths, duplicated fields, unknown variants, and cross-format conversions
- Exploit idea: make two encodings decode to semantically different values across components using the same shared type
- Invariant to test: unknown or malformed variants must fail closed before state transition logic
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing
