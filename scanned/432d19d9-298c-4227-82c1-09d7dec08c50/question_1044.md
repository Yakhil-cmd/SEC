# Q1044: registry or orchestrator: check node assignment invariants resource accounting

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/registry/canister/src/invariants/assignment.rs`::check_node_assignment_invariants with attacker-controlled node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas to make fees, cycles, memory, instructions, or refunds diverge across success, reject, trap, and retry paths; specifically, can this make validation accept a registry mutation that violates subnet, key, routing, or upgrade invariants, violating the invariant that configuration changes must be authorized, validated, and consumed consistently by all replicas, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/canister/src/invariants/assignment.rs`::check_node_assignment_invariants
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas
- Exploit idea: make validation accept a registry mutation that violates subnet, key, routing, or upgrade invariants
- Invariant to test: configuration changes must be authorized, validated, and consumed consistently by all replicas
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
