# Q1422: types packages: iter transcript configs in creation replay/idempotency

## Question
Can an unprivileged attacker enter through a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures and drive `rs/types/types/src/consensus/idkg/schnorr.rs`::iter_transcript_configs_in_creation with attacker-controlled large integers, malformed lengths, duplicated fields, unknown variants, and cross-format conversions to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this overflow, truncate, or normalize security-critical fields before authorization or accounting checks, violating the invariant that request IDs and signatures must remain bound to canonical encoded content, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `rs/types/types/src/consensus/idkg/schnorr.rs`::iter_transcript_configs_in_creation
- Entrypoint: a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures
- Attacker controls: large integers, malformed lengths, duplicated fields, unknown variants, and cross-format conversions
- Exploit idea: overflow, truncate, or normalize security-critical fields before authorization or accounting checks
- Invariant to test: request IDs and signatures must remain bound to canonical encoded content
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
