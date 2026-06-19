# Q3568: state certification: Stream Header V25 bounds/overflow

## Question
Can an unprivileged attacker enter through an ingress/canister flow mutates certified variables then requests witness generation and drive `rs/canonical_state/src/encoding/old_types.rs`::StreamHeaderV25 with attacker-controlled chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this cause checkpoint recovery to restore metadata that diverges from certified replicated state, violating the invariant that read_state witnesses must not prove stale, missing, or forged subnet/canister state, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/encoding/old_types.rs`::StreamHeaderV25
- Entrypoint: an ingress/canister flow mutates certified variables then requests witness generation
- Attacker controls: chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests
- Exploit idea: cause checkpoint recovery to restore metadata that diverges from certified replicated state
- Invariant to test: read_state witnesses must not prove stale, missing, or forged subnet/canister state
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
