# Q1093: registry or orchestrator: do update ssh readonly access for all unassigned nodes canonical encod

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/registry/canister/src/mutations/do_update_ssh_readonly_access_for_all_unassigned_nodes.rs`::do_update_ssh_readonly_access_for_all_unassigned_nodes with attacker-controlled registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this make validation accept a registry mutation that violates subnet, key, routing, or upgrade invariants, violating the invariant that registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/canister/src/mutations/do_update_ssh_readonly_access_for_all_unassigned_nodes.rs`::do_update_ssh_readonly_access_for_all_unassigned_nodes
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps
- Exploit idea: make validation accept a registry mutation that violates subnet, key, routing, or upgrade invariants
- Invariant to test: registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
