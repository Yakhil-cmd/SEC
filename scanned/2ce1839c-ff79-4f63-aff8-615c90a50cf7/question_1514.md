# Q1514: types packages: From Str resource accounting

## Question
Can an unprivileged attacker enter through a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures and drive `packages/canlog/src/types/mod.rs`::FromStr with attacker-controlled serialization round trips, canonical ordering, domain separators, and request identifiers to make fees, cycles, memory, instructions, or refunds diverge across success, reject, trap, and retry paths; specifically, can this overflow, truncate, or normalize security-critical fields before authorization or accounting checks, violating the invariant that request IDs and signatures must remain bound to canonical encoded content, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `packages/canlog/src/types/mod.rs`::FromStr
- Entrypoint: a ledger/governance/client request uses boundary numeric values, principals, accounts, hashes, or signatures
- Attacker controls: serialization round trips, canonical ordering, domain separators, and request identifiers
- Exploit idea: overflow, truncate, or normalize security-critical fields before authorization or accounting checks
- Invariant to test: request IDs and signatures must remain bound to canonical encoded content
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing
