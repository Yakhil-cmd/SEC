# Q3692: state certification: verify certified data internal replay/idempotency

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `rs/certification/src/lib.rs`::verify_certified_data_internal with attacker-controlled checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this produce a witness that omits or aliases a security-critical subtree used by clients, violating the invariant that read_state witnesses must not prove stale, missing, or forged subnet/canister state, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/certification/src/lib.rs`::verify_certified_data_internal
- Entrypoint: publicly reachable verification path
- Attacker controls: checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys
- Exploit idea: produce a witness that omits or aliases a security-critical subtree used by clients
- Invariant to test: read_state witnesses must not prove stale, missing, or forged subnet/canister state
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
