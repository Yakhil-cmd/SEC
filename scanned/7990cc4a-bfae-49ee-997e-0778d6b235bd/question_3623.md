# Q3623: state certification: end subtree canonical encoding

## Question
Can an unprivileged attacker enter through a malicious subnet peer delays or reorders state-sync chunks around checkpoint creation and drive `rs/canonical_state/src/subtree_visitor.rs`::end_subtree with attacker-controlled chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this cause checkpoint recovery to restore metadata that diverges from certified replicated state, violating the invariant that certified hash trees must be canonical and non-malleable for all attacker-chosen paths, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/subtree_visitor.rs`::end_subtree
- Entrypoint: a malicious subnet peer delays or reorders state-sync chunks around checkpoint creation
- Attacker controls: chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests
- Exploit idea: cause checkpoint recovery to restore metadata that diverges from certified replicated state
- Invariant to test: certified hash trees must be canonical and non-malleable for all attacker-chosen paths
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
