# Q3630: state certification: Iterator signature/domain

## Question
Can an unprivileged attacker enter through a read_state/query caller requests certified data through crafted paths and witnesses and drive `rs/canonical_state/src/subtree_visitor.rs`::Iterator with attacker-controlled checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this produce a witness that omits or aliases a security-critical subtree used by clients, violating the invariant that state sync must only install authenticated chunks matching the certified manifest, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/subtree_visitor.rs`::Iterator
- Entrypoint: a read_state/query caller requests certified data through crafted paths and witnesses
- Attacker controls: checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys
- Exploit idea: produce a witness that omits or aliases a security-critical subtree used by clients
- Invariant to test: state sync must only install authenticated chunks matching the certified manifest
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; mutate domain separators, registry versions, signer IDs, and message bytes independently
