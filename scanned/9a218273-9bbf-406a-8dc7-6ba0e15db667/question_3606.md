# Q3606: state certification: is full match rollback edge case

## Question
Can an unprivileged attacker enter through a read_state/query caller requests certified data through crafted paths and witnesses and drive `rs/canonical_state/src/size_limit_visitor.rs`::is_full_match with attacker-controlled chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this accept a state-sync chunk whose content is not bound to the manifest and height being certified, violating the invariant that state sync must only install authenticated chunks matching the certified manifest, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/size_limit_visitor.rs`::is_full_match
- Entrypoint: a read_state/query caller requests certified data through crafted paths and witnesses
- Attacker controls: chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests
- Exploit idea: accept a state-sync chunk whose content is not bound to the manifest and height being certified
- Invariant to test: state sync must only install authenticated chunks matching the certified manifest
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
