# Q967: registry or orchestrator: calculate rewards for date ordering/race

## Question
Can an unprivileged attacker enter through an orchestrator consumes registry updates, CUPs, upgrade images, and node configuration from replicated state and drive `rs/node_rewards/rewards_calculation/src/performance_based_algorithm/v1.rs`::calculate_rewards_for_date with attacker-controlled NNS-approved but attacker-crafted record fields, ordering of deltas, and retry timing around upgrades to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this double-apply or skip a rewards/configuration transition due to malformed registry-derived state, violating the invariant that node and boundary records must not allow unauthorized participation or reward/accounting effects, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/node_rewards/rewards_calculation/src/performance_based_algorithm/v1.rs`::calculate_rewards_for_date
- Entrypoint: an orchestrator consumes registry updates, CUPs, upgrade images, and node configuration from replicated state
- Attacker controls: NNS-approved but attacker-crafted record fields, ordering of deltas, and retry timing around upgrades
- Exploit idea: double-apply or skip a rewards/configuration transition due to malformed registry-derived state
- Invariant to test: node and boundary records must not allow unauthorized participation or reward/accounting effects
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
