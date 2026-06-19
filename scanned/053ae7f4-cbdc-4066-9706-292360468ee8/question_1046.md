# Q1046: registry or orchestrator: source rollback edge case

## Question
Can an unprivileged attacker enter through a node/operator registration flow supplies keys, endpoints, rewards data, or registry records and drive `rs/registry/canister/src/invariants/common.rs`::source with attacker-controlled node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior, violating the invariant that orchestrator behavior must be deterministic for a given certified registry version, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/canister/src/invariants/common.rs`::source
- Entrypoint: a node/operator registration flow supplies keys, endpoints, rewards data, or registry records
- Attacker controls: node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas
- Exploit idea: bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior
- Invariant to test: orchestrator behavior must be deterministic for a given certified registry version
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
