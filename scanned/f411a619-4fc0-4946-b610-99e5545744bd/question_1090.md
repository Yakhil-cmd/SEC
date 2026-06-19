# Q1090: registry or orchestrator: do update node operator config directly signature/domain

## Question
Can an unprivileged attacker enter through a node/operator registration flow supplies keys, endpoints, rewards data, or registry records and drive `rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs`::do_update_node_operator_config_directly with attacker-controlled registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior, violating the invariant that orchestrator behavior must be deterministic for a given certified registry version, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/canister/src/mutations/do_update_node_operator_config_directly.rs`::do_update_node_operator_config_directly
- Entrypoint: a node/operator registration flow supplies keys, endpoints, rewards data, or registry records
- Attacker controls: registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps
- Exploit idea: bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior
- Invariant to test: orchestrator behavior must be deterministic for a given certified registry version
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records; mutate domain separators, registry versions, signer IDs, and message bytes independently
