# Q1182: state certification: from checkpoint replay/idempotency

## Question
Can an unprivileged attacker enter through a read_state/query caller requests certified data through crafted paths and witnesses and drive `rs/replicated_state/src/canister_state/system_state/log_memory_store/mod.rs`::from_checkpoint with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this make two different states share an accepted manifest/hash-tree witness under edge-case labels, violating the invariant that state sync must only install authenticated chunks matching the certified manifest, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/replicated_state/src/canister_state/system_state/log_memory_store/mod.rs`::from_checkpoint
- Entrypoint: a read_state/query caller requests certified data through crafted paths and witnesses
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: make two different states share an accepted manifest/hash-tree witness under edge-case labels
- Invariant to test: state sync must only install authenticated chunks matching the certified manifest
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
