# Q3637: state certification: expected metadata ordering/race

## Question
Can an unprivileged attacker enter through a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata and drive `rs/canonical_state/src/traversal.rs`::expected_metadata with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this cause checkpoint recovery to restore metadata that diverges from certified replicated state, violating the invariant that checkpoint and certification versions must not create cross-version state ambiguity, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/traversal.rs`::expected_metadata
- Entrypoint: a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: cause checkpoint recovery to restore metadata that diverges from certified replicated state
- Invariant to test: checkpoint and certification versions must not create cross-version state ambiguity
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
