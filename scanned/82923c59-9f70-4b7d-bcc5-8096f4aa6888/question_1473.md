# Q1473: types packages: set default version canonical encoding

## Question
Can an unprivileged attacker enter through a public API caller submits encoded Candid/CBOR/protobuf values that decode into shared protocol types and drive `rs/types/types/src/replica_version.rs`::set_default_version with attacker-controlled encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this overflow, truncate, or normalize security-critical fields before authorization or accounting checks, violating the invariant that unknown or malformed variants must fail closed before state transition logic, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `rs/types/types/src/replica_version.rs`::set_default_version
- Entrypoint: a public API caller submits encoded Candid/CBOR/protobuf values that decode into shared protocol types
- Attacker controls: encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures
- Exploit idea: overflow, truncate, or normalize security-critical fields before authorization or accounting checks
- Invariant to test: unknown or malformed variants must fail closed before state transition logic
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
