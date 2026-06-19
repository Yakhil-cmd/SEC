# Q3600: state certification: invert routing table signature/domain

## Question
Can an unprivileged attacker enter through an ingress/canister flow mutates certified variables then requests witness generation and drive `rs/canonical_state/src/lazy_tree_conversion.rs`::invert_routing_table with attacker-controlled checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this accept a state-sync chunk whose content is not bound to the manifest and height being certified, violating the invariant that read_state witnesses must not prove stale, missing, or forged subnet/canister state, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/lazy_tree_conversion.rs`::invert_routing_table
- Entrypoint: an ingress/canister flow mutates certified variables then requests witness generation
- Attacker controls: checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys
- Exploit idea: accept a state-sync chunk whose content is not bound to the manifest and height being certified
- Invariant to test: read_state witnesses must not prove stale, missing, or forged subnet/canister state
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; mutate domain separators, registry versions, signer IDs, and message bytes independently
