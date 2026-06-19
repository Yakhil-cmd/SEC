# Q1197: state certification: is non zero ordering/race

## Question
Can an unprivileged attacker enter through a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata and drive `rs/replicated_state/src/metadata_state/subnet_schedule.rs`::is_non_zero with attacker-controlled checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this accept a state-sync chunk whose content is not bound to the manifest and height being certified, violating the invariant that checkpoint and certification versions must not create cross-version state ambiguity, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/replicated_state/src/metadata_state/subnet_schedule.rs`::is_non_zero
- Entrypoint: a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata
- Attacker controls: checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys
- Exploit idea: accept a state-sync chunk whose content is not bound to the manifest and height being certified
- Invariant to test: checkpoint and certification versions must not create cross-version state ambiguity
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
