# Q1489: types packages: get sink certification/witness

## Question
Can an unprivileged attacker enter through a public API caller submits encoded Candid/CBOR/protobuf values that decode into shared protocol types and drive `packages/canlog/src/lib.rs`::get_sink with attacker-controlled serialization round trips, canonical ordering, domain separators, and request identifiers to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this confuse canonical hashing/signing by changing field order, unknown fields, or conversion defaults, violating the invariant that unknown or malformed variants must fail closed before state transition logic, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/canlog/src/lib.rs`::get_sink
- Entrypoint: a public API caller submits encoded Candid/CBOR/protobuf values that decode into shared protocol types
- Attacker controls: serialization round trips, canonical ordering, domain separators, and request identifiers
- Exploit idea: confuse canonical hashing/signing by changing field order, unknown fields, or conversion defaults
- Invariant to test: unknown or malformed variants must fail closed before state transition logic
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing
