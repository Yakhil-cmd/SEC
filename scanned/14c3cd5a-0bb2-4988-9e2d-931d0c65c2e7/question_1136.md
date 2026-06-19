# Q1136: registry or orchestrator: get ecdsa key id from signing subnet list key rollback edge case

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/registry/keys/src/lib.rs`::get_ecdsa_key_id_from_signing_subnet_list_key with attacker-controlled node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior, violating the invariant that configuration changes must be authorized, validated, and consumed consistently by all replicas, and produce HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss?

## Target
- File/function: `rs/registry/keys/src/lib.rs`::get_ecdsa_key_id_from_signing_subnet_list_key
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: node public keys, TLS certs, endpoints, proposal payloads, upgrade metadata, and registry deltas
- Exploit idea: bypass node-key or endpoint validation to inject a record that affects consensus/boundary behavior
- Invariant to test: configuration changes must be authorized, validated, and consumed consistently by all replicas
- Expected HackenProof impact: HackenProof Critical/High: consensus configuration compromise, unauthorized node/governance effect, or significant infrastructure integrity loss
- Fast validation: construct registry mutation/orchestrator-consumer tests and assert invariant validation rejects malformed records
