# Q976: registry or orchestrator: main rollback edge case

## Question
Can an unprivileged attacker enter through a boundary/node rewards caller submits public canister/API requests affecting registry-derived state and drive `rs/orchestrator/registry_replicator/src/main.rs`::main with attacker-controlled node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior, violating the invariant that configuration changes must be authorized, validated, and consumed consistently by all replicas, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/orchestrator/registry_replicator/src/main.rs`::main
- Entrypoint: a boundary/node rewards caller submits public canister/API requests affecting registry-derived state
- Attacker controls: node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas
- Exploit idea: bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior
- Invariant to test: configuration changes must be authorized, validated, and consumed consistently by all replicas
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
