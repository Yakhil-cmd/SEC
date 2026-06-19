# Q987: registry or orchestrator: main ordering/race

## Question
Can an unprivileged attacker enter through an orchestrator consumes registry updates, CUPs, upgrade images, and node configuration from replicated state and drive `rs/orchestrator/src/main.rs`::main with attacker-controlled registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this cause orchestrator or registry consumers to interpret the same record differently across versions, violating the invariant that node and boundary records must not allow unauthorized participation or reward/accounting effects, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/orchestrator/src/main.rs`::main
- Entrypoint: an orchestrator consumes registry updates, CUPs, upgrade images, and node configuration from replicated state
- Attacker controls: registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps
- Exploit idea: cause orchestrator or registry consumers to interpret the same record differently across versions
- Invariant to test: node and boundary records must not allow unauthorized participation or reward/accounting effects
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
