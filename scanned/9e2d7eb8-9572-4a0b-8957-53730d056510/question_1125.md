# Q1125: registry or orchestrator: get ecdsa signing subnets cross module mismatch

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/registry/helpers/src/ecdsa_keys.rs`::get_ecdsa_signing_subnets with attacker-controlled registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this cause orchestrator or registry consumers to interpret the same record differently across versions, violating the invariant that registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/helpers/src/ecdsa_keys.rs`::get_ecdsa_signing_subnets
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: registry mutation payloads, node records, subnet membership, routing tables, replica versions, rewards data, and timestamps
- Exploit idea: cause orchestrator or registry consumers to interpret the same record differently across versions
- Invariant to test: registry mutations must preserve subnet membership, routing, key, version, and upgrade safety invariants
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
