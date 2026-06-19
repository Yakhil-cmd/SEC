# Q1470: types packages: name signature/domain

## Question
Can an unprivileged attacker enter through a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures and drive `rs/types/types/src/methods.rs`::name with attacker-controlled encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this confuse canonical hashing/signing by changing field order, unknown fields, or conversion defaults, violating the invariant that request IDs and signatures must remain bound to canonical encoded content, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `rs/types/types/src/methods.rs`::name
- Entrypoint: a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures
- Attacker controls: encoded bytes, enum variants, optional fields, principals, account IDs, amounts, timestamps, hashes, and signatures
- Exploit idea: confuse canonical hashing/signing by changing field order, unknown fields, or conversion defaults
- Invariant to test: request IDs and signatures must remain bound to canonical encoded content
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; mutate domain separators, registry versions, signer IDs, and message bytes independently
