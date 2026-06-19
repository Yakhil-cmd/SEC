# Q3576: state certification: Request Metadata rollback edge case

## Question
Can an unprivileged attacker enter through an ingress/canister flow mutates certified variables then requests witness generation and drive `rs/canonical_state/src/encoding/types.rs`::RequestMetadata with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this cause checkpoint recovery to restore metadata that diverges from certified replicated state, violating the invariant that read_state witnesses must not prove stale, missing, or forged subnet/canister state, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/encoding/types.rs`::RequestMetadata
- Entrypoint: an ingress/canister flow mutates certified variables then requests witness generation
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: cause checkpoint recovery to restore metadata that diverges from certified replicated state
- Invariant to test: read_state witnesses must not prove stale, missing, or forged subnet/canister state
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
