# Q3635: state certification: encode metadata cross module mismatch

## Question
Can an unprivileged attacker enter through a malicious subnet peer delays or reorders state-sync chunks around checkpoint creation and drive `rs/canonical_state/src/traversal.rs`::encode_metadata with attacker-controlled checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this accept a state-sync chunk whose content is not bound to the manifest and height being certified, violating the invariant that certified hash trees must be canonical and non-malleable for all attacker-chosen paths, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/traversal.rs`::encode_metadata
- Entrypoint: a malicious subnet peer delays or reorders state-sync chunks around checkpoint creation
- Attacker controls: checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys
- Exploit idea: accept a state-sync chunk whose content is not bound to the manifest and height being certified
- Invariant to test: certified hash trees must be canonical and non-malleable for all attacker-chosen paths
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
