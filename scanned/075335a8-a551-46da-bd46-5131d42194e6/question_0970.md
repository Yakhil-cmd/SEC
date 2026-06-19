# Q970: registry or orchestrator: run signature/domain

## Question
Can an unprivileged attacker enter through a node/operator registration flow supplies keys, endpoints, rewards data, or registry records and drive `rs/orchestrator/dashboard/src/dashboard.rs`::run with attacker-controlled registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this double-apply or skip a rewards/configuration transition due to malformed registry-derived state, violating the invariant that orchestrator behavior must be deterministic for a given certified registry version, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/orchestrator/dashboard/src/dashboard.rs`::run
- Entrypoint: a node/operator registration flow supplies keys, endpoints, rewards data, or registry records
- Attacker controls: registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps
- Exploit idea: double-apply or skip a rewards/configuration transition due to malformed registry-derived state
- Invariant to test: orchestrator behavior must be deterministic for a given certified registry version
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records; mutate domain separators, registry versions, signer IDs, and message bytes independently
