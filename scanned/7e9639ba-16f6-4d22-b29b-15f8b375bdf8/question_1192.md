# Q1192: state certification: observe broken soft invariant replay/idempotency

## Question
Can an unprivileged attacker enter through an ingress/canister flow mutates certified variables then requests witness generation and drive `rs/replicated_state/src/lib.rs`::observe_broken_soft_invariant with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this accept a state-sync chunk whose content is not bound to the manifest and height being certified, violating the invariant that read_state witnesses must not prove stale, missing, or forged subnet/canister state, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/replicated_state/src/lib.rs`::observe_broken_soft_invariant
- Entrypoint: an ingress/canister flow mutates certified variables then requests witness generation
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: accept a state-sync chunk whose content is not bound to the manifest and height being certified
- Invariant to test: read_state witnesses must not prove stale, missing, or forged subnet/canister state
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
