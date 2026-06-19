# Q3602: state certification: replicated state as lazy tree replay/idempotency

## Question
Can an unprivileged attacker enter through a read_state/query caller requests certified data through crafted paths and witnesses and drive `rs/canonical_state/src/lazy_tree_conversion.rs`::replicated_state_as_lazy_tree with attacker-controlled chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this cause checkpoint recovery to restore metadata that diverges from certified replicated state, violating the invariant that state sync must only install authenticated chunks matching the certified manifest, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/lazy_tree_conversion.rs`::replicated_state_as_lazy_tree
- Entrypoint: a read_state/query caller requests certified data through crafted paths and witnesses
- Attacker controls: chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests
- Exploit idea: cause checkpoint recovery to restore metadata that diverges from certified replicated state
- Invariant to test: state sync must only install authenticated chunks matching the certified manifest
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
