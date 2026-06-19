# Q1102: registry or orchestrator: do add node replay/idempotency

## Question
Can an unprivileged attacker enter through a node/operator registration flow supplies keys, endpoints, rewards data, or registry records and drive `rs/registry/canister/src/mutations/node_management/do_add_node.rs`::do_add_node with attacker-controlled NNS-approved but attacker-crafted record fields, ordering of deltas, and retry timing around upgrades to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior, violating the invariant that orchestrator behavior must be deterministic for a given certified registry version, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/canister/src/mutations/node_management/do_add_node.rs`::do_add_node
- Entrypoint: a node/operator registration flow supplies keys, endpoints, rewards data, or registry records
- Attacker controls: NNS-approved but attacker-crafted record fields, ordering of deltas, and retry timing around upgrades
- Exploit idea: bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior
- Invariant to test: orchestrator behavior must be deterministic for a given certified registry version
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
