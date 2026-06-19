# Q1048: registry or orchestrator: check endpoint invariants bounds/overflow

## Question
Can an unprivileged attacker enter through a boundary/node rewards caller submits public canister/API requests affecting registry-derived state and drive `rs/registry/canister/src/invariants/endpoint.rs`::check_endpoint_invariants with attacker-controlled node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior, violating the invariant that configuration changes must be authorized, validated, and consumed consistently by all replicas, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/canister/src/invariants/endpoint.rs`::check_endpoint_invariants
- Entrypoint: a boundary/node rewards caller submits public canister/API requests affecting registry-derived state
- Attacker controls: node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas
- Exploit idea: bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior
- Invariant to test: configuration changes must be authorized, validated, and consumed consistently by all replicas
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
