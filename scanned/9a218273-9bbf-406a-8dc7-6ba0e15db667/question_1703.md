# Q1703: types packages: Cbor Principal canonical encoding

## Question
Can an unprivileged attacker enter through a canister or API request crosses serialization boundaries using attacker-controlled bytes and optional fields and drive `packages/icrc-cbor/src/principal.rs`::CborPrincipal with attacker-controlled serialization round trips, canonical ordering, domain separators, and request identifiers to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this overflow, truncate, or normalize security-critical fields before authorization or accounting checks, violating the invariant that shared protocol types must serialize, hash, compare, and validate identically across all consumers, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/icrc-cbor/src/principal.rs`::CborPrincipal
- Entrypoint: a canister or API request crosses serialization boundaries using attacker-controlled bytes and optional fields
- Attacker controls: serialization round trips, canonical ordering, domain separators, and request identifiers
- Exploit idea: overflow, truncate, or normalize security-critical fields before authorization or accounting checks
- Invariant to test: shared protocol types must serialize, hash, compare, and validate identically across all consumers
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
