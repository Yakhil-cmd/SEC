# Q1403: types packages: id canonical encoding

## Question
Can an unprivileged attacker enter through a canister or API request crosses serialization boundaries using attacker-controlled bytes and optional fields and drive `rs/types/types/src/artifact.rs`::id with attacker-controlled encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this make two encodings decode to semantically different values across components using the same shared type, violating the invariant that shared protocol types must serialize, hash, compare, and validate identically across all consumers, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `rs/types/types/src/artifact.rs`::id
- Entrypoint: a canister or API request crosses serialization boundaries using attacker-controlled bytes and optional fields
- Attacker controls: encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures
- Exploit idea: make two encodings decode to semantically different values across components using the same shared type
- Invariant to test: shared protocol types must serialize, hash, compare, and validate identically across all consumers
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
