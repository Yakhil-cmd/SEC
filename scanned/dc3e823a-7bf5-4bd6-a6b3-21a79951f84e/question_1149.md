# Q1149: registry or orchestrator: From certification/witness

## Question
Can an unprivileged attacker enter through a governance proposal or registry client submits node, subnet, routing-table, replica-version, or firewall mutations and drive `rs/registry/routing_table/src/proto.rs`::From with attacker-controlled registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior, violating the invariant that registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/routing_table/src/proto.rs`::From
- Entrypoint: a governance proposal or registry client submits node, subnet, routing-table, replica-version, or firewall mutations
- Attacker controls: registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps
- Exploit idea: bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior
- Invariant to test: registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
