# Q21: types packages: decode authorization boundary

## Question
Can an unprivileged attacker enter through a public API caller submits encoded Candid/CBOR/protobuf values that decode into shared protocol types and drive `packages/icrc-cbor/src/principal.rs`::decode with attacker-controlled serialization round trips, canonical ordering, domain separators, and request identifiers to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this accept a boundary type value that downstream consensus/ledger/governance logic assumes impossible, violating the invariant that unknown or malformed variants must fail closed before state transition logic, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/icrc-cbor/src/principal.rs`::decode
- Entrypoint: a public API caller submits encoded Candid/CBOR/protobuf values that decode into shared protocol types
- Attacker controls: serialization round trips, canonical ordering, domain separators, and request identifiers
- Exploit idea: accept a boundary type value that downstream consensus/ledger/governance logic assumes impossible
- Invariant to test: unknown or malformed variants must fail closed before state transition logic
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing
