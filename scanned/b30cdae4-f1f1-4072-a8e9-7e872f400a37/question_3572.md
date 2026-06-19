# Q3572: state certification: try from deltas replay/idempotency

## Question
Can an unprivileged attacker enter through an ingress/canister flow mutates certified variables then requests witness generation and drive `rs/canonical_state/src/encoding/types.rs`::try_from_deltas with attacker-controlled checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this cause checkpoint recovery to restore metadata that diverges from certified replicated state, violating the invariant that read_state witnesses must not prove stale, missing, or forged subnet/canister state, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/encoding/types.rs`::try_from_deltas
- Entrypoint: an ingress/canister flow mutates certified variables then requests witness generation
- Attacker controls: checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys
- Exploit idea: cause checkpoint recovery to restore metadata that diverges from certified replicated state
- Invariant to test: read_state witnesses must not prove stale, missing, or forged subnet/canister state
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
