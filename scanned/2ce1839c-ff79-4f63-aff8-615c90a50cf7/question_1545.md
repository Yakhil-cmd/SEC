# Q1545: types packages: safe truncate right cross module mismatch

## Question
Can an unprivileged attacker enter through a public API caller submits encoded Candid/CBOR/protobuf values that decode into shared protocol types and drive `packages/ic-error-types/src/lib.rs`::safe_truncate_right with attacker-controlled encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this confuse canonical hashing/signing by changing field order, unknown fields, or conversion defaults, violating the invariant that unknown or malformed variants must fail closed before state transition logic, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/ic-error-types/src/lib.rs`::safe_truncate_right
- Entrypoint: a public API caller submits encoded Candid/CBOR/protobuf values that decode into shared protocol types
- Attacker controls: encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures
- Exploit idea: confuse canonical hashing/signing by changing field order, unknown fields, or conversion defaults
- Invariant to test: unknown or malformed variants must fail closed before state transition logic
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing
