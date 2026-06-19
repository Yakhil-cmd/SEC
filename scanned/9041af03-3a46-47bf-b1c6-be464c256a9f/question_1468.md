# Q1468: types packages: id bounds/overflow

## Question
Can an unprivileged attacker enter through a Rosetta/ICRC/management-canister caller supplies edge-case type values that flow into production logic and drive `rs/types/types/src/messages/read_state.rs`::id with attacker-controlled serialization round trips, canonical ordering, domain separators, and request identifiers to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this make two encodings decode to semantically different values across components using the same shared type, violating the invariant that numeric/account/principal conversions must not truncate, alias, or bypass authorization/accounting checks, and produce HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash?

## Target
- File/function: `rs/types/types/src/messages/read_state.rs`::id
- Entrypoint: a Rosetta/ICRC/management-canister caller supplies edge-case type values that flow into production logic
- Attacker controls: serialization round trips, canonical ordering, domain separators, and request identifiers
- Exploit idea: make two encodings decode to semantically different values across components using the same shared type
- Invariant to test: numeric/account/principal conversions must not truncate, alias, or bypass authorization/accounting checks
- Expected HackenProof impact: HackenProof High/Medium: authorization bypass, accounting mismatch, signature confusion, or production platform crash
- Fast validation: fuzz type round trips and differential consumers, then assert canonical equality, rejection, and no overflow/aliasing; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
