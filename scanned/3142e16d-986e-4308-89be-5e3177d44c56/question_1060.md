# Q1060: registry or orchestrator: get subnet ids from subnet list signature/domain

## Question
Can an unprivileged attacker enter through a boundary/node rewards caller submits public canister/API requests affecting registry-derived state and drive `rs/registry/canister/src/mutations/common.rs`::get_subnet_ids_from_subnet_list with attacker-controlled node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this double-apply or skip a rewards/configuration transition due to malformed registry-derived state, violating the invariant that configuration changes must be authorized, validated, and consumed consistently by all replicas, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/canister/src/mutations/common.rs`::get_subnet_ids_from_subnet_list
- Entrypoint: a boundary/node rewards caller submits public canister/API requests affecting registry-derived state
- Attacker controls: node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas
- Exploit idea: double-apply or skip a rewards/configuration transition due to malformed registry-derived state
- Invariant to test: configuration changes must be authorized, validated, and consumed consistently by all replicas
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records; mutate domain separators, registry versions, signer IDs, and message bytes independently
