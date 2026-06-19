# Q949: registry or orchestrator: Try From certification/witness

## Question
Can an unprivileged attacker enter through a governance proposal or registry client submits node, subnet, routing-table, replica-version, or firewall mutations and drive `rs/node_rewards/canister/api/src/api_native_conversion.rs`::TryFrom with attacker-controlled node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this cause orchestrator or registry consumers to interpret the same record differently across versions, violating the invariant that registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/node_rewards/canister/api/src/api_native_conversion.rs`::TryFrom
- Entrypoint: a governance proposal or registry client submits node, subnet, routing-table, replica-version, or firewall mutations
- Attacker controls: node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas
- Exploit idea: cause orchestrator or registry consumers to interpret the same record differently across versions
- Invariant to test: registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
