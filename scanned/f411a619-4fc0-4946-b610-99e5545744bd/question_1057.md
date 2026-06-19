# Q1057: registry or orchestrator: check unassigned nodes config invariants ordering/race

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/registry/canister/src/invariants/unassigned_nodes_config.rs`::check_unassigned_nodes_config_invariants with attacker-controlled node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior, violating the invariant that registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/canister/src/invariants/unassigned_nodes_config.rs`::check_unassigned_nodes_config_invariants
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas
- Exploit idea: bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior
- Invariant to test: registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
