# Q1097: registry or orchestrator: do update unassigned nodes config ordering/race

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/registry/canister/src/mutations/do_update_unassigned_nodes_config.rs`::do_update_unassigned_nodes_config with attacker-controlled node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this double-apply or skip a rewards/configuration transition due to malformed registry-derived state, violating the invariant that registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/canister/src/mutations/do_update_unassigned_nodes_config.rs`::do_update_unassigned_nodes_config
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas
- Exploit idea: double-apply or skip a rewards/configuration transition due to malformed registry-derived state
- Invariant to test: registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
