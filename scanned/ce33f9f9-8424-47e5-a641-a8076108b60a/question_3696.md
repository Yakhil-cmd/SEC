# Q3696: state certification: verify certificate internal rollback edge case

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `rs/certification/src/lib.rs`::verify_certificate_internal with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this produce a witness that omits or aliases a security-critical subtree used by clients, violating the invariant that read_state witnesses must not prove stale, missing, or forged subnet/canister state, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/certification/src/lib.rs`::verify_certificate_internal
- Entrypoint: publicly reachable verification path
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: produce a witness that omits or aliases a security-critical subtree used by clients
- Invariant to test: read_state witnesses must not prove stale, missing, or forged subnet/canister state
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
