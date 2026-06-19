# Q3550: state certification: encode stream header signature/domain

## Question
Can an unprivileged attacker enter through a read_state/query caller requests certified data through crafted paths and witnesses and drive `rs/canonical_state/src/encoding.rs`::encode_stream_header with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this cause checkpoint recovery to restore metadata that diverges from certified replicated state, violating the invariant that state sync must only install authenticated chunks matching the certified manifest, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/encoding.rs`::encode_stream_header
- Entrypoint: a read_state/query caller requests certified data through crafted paths and witnesses
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: cause checkpoint recovery to restore metadata that diverges from certified replicated state
- Invariant to test: state sync must only install authenticated chunks matching the certified manifest
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; mutate domain separators, registry versions, signer IDs, and message bytes independently
