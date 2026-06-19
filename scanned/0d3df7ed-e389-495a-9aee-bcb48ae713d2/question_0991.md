# Q991: registry or orchestrator: register node authorization boundary

## Question
Can an unprivileged attacker enter through an orchestrator consumes registry updates, CUPs, upgrade images, and node configuration from replicated state and drive `rs/orchestrator/src/registration.rs`::register_node with attacker-controlled node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this cause orchestrator or registry consumers to interpret the same record differently across versions, violating the invariant that node and boundary records must not allow unauthorized participation or reward/accounting effects, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/orchestrator/src/registration.rs`::register_node
- Entrypoint: an orchestrator consumes registry updates, CUPs, upgrade images, and node configuration from replicated state
- Attacker controls: node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas
- Exploit idea: cause orchestrator or registry consumers to interpret the same record differently across versions
- Invariant to test: node and boundary records must not allow unauthorized participation or reward/accounting effects
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
