# Q3569: state certification: From certification/witness

## Question
Can an unprivileged attacker enter through a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata and drive `rs/canonical_state/src/encoding/old_types.rs`::From with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this make two different states share an accepted manifest/hash-tree witness under edge-case labels, violating the invariant that checkpoint and certification versions must not create cross-version state ambiguity, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/encoding/old_types.rs`::From
- Entrypoint: a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: make two different states share an accepted manifest/hash-tree witness under edge-case labels
- Invariant to test: checkpoint and certification versions must not create cross-version state ambiguity
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
