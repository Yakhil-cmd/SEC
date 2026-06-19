# Q1507: types packages: Regex String ordering/race

## Question
Can an unprivileged attacker enter through a canister or API request crosses serialization boundaries using attacker-controlled bytes and optional fields and drive `packages/canlog/src/types/mod.rs`::RegexString with attacker-controlled serialization round trips, canonical ordering, domain separators, and request identifiers to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this confuse canonical hashing/signing by changing field order, unknown fields, or conversion defaults, violating the invariant that shared protocol types must serialize, hash, compare, and validate identically across all consumers, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/canlog/src/types/mod.rs`::RegexString
- Entrypoint: a canister or API request crosses serialization boundaries using attacker-controlled bytes and optional fields
- Attacker controls: serialization round trips, canonical ordering, domain separators, and request identifiers
- Exploit idea: confuse canonical hashing/signing by changing field order, unknown fields, or conversion defaults
- Invariant to test: shared protocol types must serialize, hash, compare, and validate identically across all consumers
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing
