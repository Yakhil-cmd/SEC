# Q1390: types packages: Too Long signature/domain

## Question
Can an unprivileged attacker enter through a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures and drive `rs/types/base_types/src/principal_id.rs`::TooLong with attacker-controlled large integers, malformed lengths, duplicated fields, unknown variants, and cross-format conversions to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this make two encodings decode to semantically different values across components using the same shared type, violating the invariant that request IDs and signatures must remain bound to canonical encoded content, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `rs/types/base_types/src/principal_id.rs`::TooLong
- Entrypoint: a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures
- Attacker controls: large integers, malformed lengths, duplicated fields, unknown variants, and cross-format conversions
- Exploit idea: make two encodings decode to semantically different values across components using the same shared type
- Invariant to test: request IDs and signatures must remain bound to canonical encoded content
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; mutate domain separators, registry versions, signer IDs, and message bytes independently
