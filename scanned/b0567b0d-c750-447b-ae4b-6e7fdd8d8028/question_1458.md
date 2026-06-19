# Q1458: types packages: user id into protobuf bounds/overflow

## Question
Can an unprivileged attacker enter through a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures and drive `rs/types/types/src/lib.rs`::user_id_into_protobuf with attacker-controlled serialization round trips, canonical ordering, domain separators, and request identifiers to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this confuse canonical hashing/signing by changing field order, unknown fields, or conversion defaults, violating the invariant that request IDs and signatures must remain bound to canonical encoded content, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `rs/types/types/src/lib.rs`::user_id_into_protobuf
- Entrypoint: a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures
- Attacker controls: serialization round trips, canonical ordering, domain separators, and request identifiers
- Exploit idea: confuse canonical hashing/signing by changing field order, unknown fields, or conversion defaults
- Invariant to test: request IDs and signatures must remain bound to canonical encoded content
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
