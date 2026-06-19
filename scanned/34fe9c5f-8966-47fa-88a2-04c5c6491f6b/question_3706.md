# Q3706: state certification: Replica State rollback edge case

## Question
Can an unprivileged attacker enter through a read_state/query caller requests certified data through crafted paths and witnesses and drive `rs/certification/src/lib.rs`::ReplicaState with attacker-controlled chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this make two different states share an accepted manifest/hash-tree witness under edge-case labels, violating the invariant that state sync must only install authenticated chunks matching the certified manifest, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/certification/src/lib.rs`::ReplicaState
- Entrypoint: a read_state/query caller requests certified data through crafted paths and witnesses
- Attacker controls: chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests
- Exploit idea: make two different states share an accepted manifest/hash-tree witness under edge-case labels
- Invariant to test: state sync must only install authenticated chunks matching the certified manifest
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
