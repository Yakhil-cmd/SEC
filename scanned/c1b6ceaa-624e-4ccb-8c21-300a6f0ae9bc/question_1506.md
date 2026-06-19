# Q1506: types packages: from str rollback edge case

## Question
Can an unprivileged attacker enter through a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures and drive `packages/canlog/src/types/mod.rs`::from_str with attacker-controlled large integers, malformed lengths, duplicated fields, unknown variants, and cross-format conversions to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this overflow, truncate, or normalize security-critical fields before authorization or accounting checks, violating the invariant that request IDs and signatures must remain bound to canonical encoded content, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/canlog/src/types/mod.rs`::from_str
- Entrypoint: a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures
- Attacker controls: large integers, malformed lengths, duplicated fields, unknown variants, and cross-format conversions
- Exploit idea: overflow, truncate, or normalize security-critical fields before authorization or accounting checks
- Invariant to test: request IDs and signatures must remain bound to canonical encoded content
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing
