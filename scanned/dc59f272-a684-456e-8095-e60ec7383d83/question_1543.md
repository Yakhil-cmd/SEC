# Q1543: types packages: reject code from error code canonical encoding

## Question
Can an unprivileged attacker enter through a canister or API request crosses serialization boundaries using attacker-controlled bytes and optional fields and drive `packages/ic-error-types/src/lib.rs`::reject_code_from_error_code with attacker-controlled encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this make two encodings decode to semantically different values across components using the same shared type, violating the invariant that shared protocol types must serialize, hash, compare, and validate identically across all consumers, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/ic-error-types/src/lib.rs`::reject_code_from_error_code
- Entrypoint: a canister or API request crosses serialization boundaries using attacker-controlled bytes and optional fields
- Attacker controls: encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures
- Exploit idea: make two encodings decode to semantically different values across components using the same shared type
- Invariant to test: shared protocol types must serialize, hash, compare, and validate identically across all consumers
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
