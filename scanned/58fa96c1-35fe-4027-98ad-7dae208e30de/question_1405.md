# Q1405: types packages: callback id cross module mismatch

## Question
Can an unprivileged attacker enter through public call/ingress endpoint and drive `rs/types/types/src/batch/canister_http.rs`::callback_id with attacker-controlled serialization round trips, canonical ordering, domain separators, and request identifiers to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this overflow, truncate, or normalize security-critical fields before authorization or accounting checks, violating the invariant that unknown or malformed variants must fail closed before state transition logic, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `rs/types/types/src/batch/canister_http.rs`::callback_id
- Entrypoint: public call/ingress endpoint
- Attacker controls: serialization round trips, canonical ordering, domain separators, and request identifiers
- Exploit idea: overflow, truncate, or normalize security-critical fields before authorization or accounting checks
- Invariant to test: unknown or malformed variants must fail closed before state transition logic
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing
