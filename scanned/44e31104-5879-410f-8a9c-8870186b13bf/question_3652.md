# Q3652: state certification: not found replay/idempotency

## Question
Can an unprivileged attacker enter through an ingress/canister flow mutates certified variables then requests witness generation and drive `rs/canonical_state/src/visitor.rs`::not_found with attacker-controlled chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this make two different states share an accepted manifest/hash-tree witness under edge-case labels, violating the invariant that read_state witnesses must not prove stale, missing, or forged subnet/canister state, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/visitor.rs`::not_found
- Entrypoint: an ingress/canister flow mutates certified variables then requests witness generation
- Attacker controls: chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests
- Exploit idea: make two different states share an accepted manifest/hash-tree witness under edge-case labels
- Invariant to test: read_state witnesses must not prove stale, missing, or forged subnet/canister state
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
