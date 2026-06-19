# Q975: registry or orchestrator: poll once cross module mismatch

## Question
Can an unprivileged attacker enter through an orchestrator consumes registry updates, CUPs, upgrade images, and node configuration from replicated state and drive `rs/orchestrator/registry_replicator/src/lib.rs`::poll_once with attacker-controlled NNS-approved but attacker-crafted record fields, ordering of deltas, and retry timing around upgrades to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this make validation accept a registry mutation that violates subnet, key, routing, or upgrade invariants, violating the invariant that node and boundary records must not allow unauthorized participation or reward/accounting effects, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/orchestrator/registry_replicator/src/lib.rs`::poll_once
- Entrypoint: an orchestrator consumes registry updates, CUPs, upgrade images, and node configuration from replicated state
- Attacker controls: NNS-approved but attacker-crafted record fields, ordering of deltas, and retry timing around upgrades
- Exploit idea: make validation accept a registry mutation that violates subnet, key, routing, or upgrade invariants
- Invariant to test: node and boundary records must not allow unauthorized participation or reward/accounting effects
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
