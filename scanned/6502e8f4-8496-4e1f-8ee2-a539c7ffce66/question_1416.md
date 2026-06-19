# Q1416: state certification: id rollback edge case

## Question
Can an unprivileged attacker enter through an ingress/canister flow mutates certified variables then requests witness generation and drive `rs/types/types/src/consensus/certification.rs`::id with attacker-controlled checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this make two different states share an accepted manifest/hash-tree witness under edge-case labels, violating the invariant that read_state witnesses must not prove stale, missing, or forged subnet/canister state, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/types/types/src/consensus/certification.rs`::id
- Entrypoint: an ingress/canister flow mutates certified variables then requests witness generation
- Attacker controls: checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys
- Exploit idea: make two different states share an accepted manifest/hash-tree witness under edge-case labels
- Invariant to test: read_state witnesses must not prove stale, missing, or forged subnet/canister state
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
