# Q1083: registry or orchestrator: name canonical encoding

## Question
Can an unprivileged attacker enter through an orchestrator consumes registry updates, CUPs, upgrade images, and node configuration from replicated state and drive `rs/registry/canister/src/mutations/do_set_subnet_operational_level.rs`::name with attacker-controlled registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this double-apply or skip a rewards/configuration transition due to malformed registry-derived state, violating the invariant that node and boundary records must not allow unauthorized participation or reward/accounting effects, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/canister/src/mutations/do_set_subnet_operational_level.rs`::name
- Entrypoint: an orchestrator consumes registry updates, CUPs, upgrade images, and node configuration from replicated state
- Attacker controls: registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps
- Exploit idea: double-apply or skip a rewards/configuration transition due to malformed registry-derived state
- Invariant to test: node and boundary records must not allow unauthorized participation or reward/accounting effects
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
