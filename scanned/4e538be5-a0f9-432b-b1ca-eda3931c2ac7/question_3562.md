# Q3562: state certification: Request V22 replay/idempotency

## Question
Can an unprivileged attacker enter through a read_state/query caller requests certified data through crafted paths and witnesses and drive `rs/canonical_state/src/encoding/old_types.rs`::RequestV22 with attacker-controlled checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this accept a state-sync chunk whose content is not bound to the manifest and height being certified, violating the invariant that state sync must only install authenticated chunks matching the certified manifest, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/encoding/old_types.rs`::RequestV22
- Entrypoint: a read_state/query caller requests certified data through crafted paths and witnesses
- Attacker controls: checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys
- Exploit idea: accept a state-sync chunk whose content is not bound to the manifest and height being certified
- Invariant to test: state sync must only install authenticated chunks matching the certified manifest
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
