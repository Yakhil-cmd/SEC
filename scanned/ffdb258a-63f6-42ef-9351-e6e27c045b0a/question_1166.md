# Q1166: state certification: peek rollback edge case

## Question
Can an unprivileged attacker enter through a read_state/query caller requests certified data through crafted paths and witnesses and drive `rs/replicated_state/src/canister_state/queues.rs`::peek with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this accept a state-sync chunk whose content is not bound to the manifest and height being certified, violating the invariant that state sync must only install authenticated chunks matching the certified manifest, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/replicated_state/src/canister_state/queues.rs`::peek
- Entrypoint: a read_state/query caller requests certified data through crafted paths and witnesses
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: accept a state-sync chunk whose content is not bound to the manifest and height being certified
- Invariant to test: state sync must only install authenticated chunks matching the certified manifest
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
