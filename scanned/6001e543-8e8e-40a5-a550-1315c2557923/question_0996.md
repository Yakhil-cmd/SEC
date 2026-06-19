# Q996: registry or orchestrator: https endpoint to url rollback edge case

## Question
Can an unprivileged attacker enter through a boundary/node rewards caller submits public canister/API requests affecting registry-derived state and drive `rs/orchestrator/src/utils.rs`::https_endpoint_to_url with attacker-controlled NNS-approved but attacker-crafted record fields, ordering of deltas, and retry timing around upgrades to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this double-apply or skip a rewards/configuration transition due to malformed registry-derived state, violating the invariant that configuration changes must be authorized, validated, and consumed consistently by all replicas, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/orchestrator/src/utils.rs`::https_endpoint_to_url
- Entrypoint: a boundary/node rewards caller submits public canister/API requests affecting registry-derived state
- Attacker controls: NNS-approved but attacker-crafted record fields, ordering of deltas, and retry timing around upgrades
- Exploit idea: double-apply or skip a rewards/configuration transition due to malformed registry-derived state
- Invariant to test: configuration changes must be authorized, validated, and consumed consistently by all replicas
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
