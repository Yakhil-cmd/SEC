# Q1702: types packages: encode replay/idempotency

## Question
Can an unprivileged attacker enter through a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures and drive `packages/icrc-cbor/src/principal.rs`::encode with attacker-controlled encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this make two encodings decode to semantically different values across components using the same shared type, violating the invariant that request IDs and signatures must remain bound to canonical encoded content, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/icrc-cbor/src/principal.rs`::encode
- Entrypoint: a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures
- Attacker controls: encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures
- Exploit idea: make two encodings decode to semantically different values across components using the same shared type
- Invariant to test: request IDs and signatures must remain bound to canonical encoded content
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
