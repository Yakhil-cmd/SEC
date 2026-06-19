# Q237: state certification: is empty ordering/race

## Question
Can an unprivileged attacker enter through a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata and drive `rs/canonical_state/src/encoding/old_types.rs`::is_empty with attacker-controlled checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this make two different states share an accepted manifest/hash-tree witness under edge-case labels, violating the invariant that checkpoint and certification versions must not create cross-version state ambiguity, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/encoding/old_types.rs`::is_empty
- Entrypoint: a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata
- Attacker controls: checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys
- Exploit idea: make two different states share an accepted manifest/hash-tree witness under edge-case labels
- Invariant to test: checkpoint and certification versions must not create cross-version state ambiguity
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
