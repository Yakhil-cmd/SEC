# Q3697: state certification: verify delegation certificate ordering/race

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `rs/certification/src/lib.rs`::verify_delegation_certificate with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this cause checkpoint recovery to restore metadata that diverges from certified replicated state, violating the invariant that checkpoint and certification versions must not create cross-version state ambiguity, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/certification/src/lib.rs`::verify_delegation_certificate
- Entrypoint: publicly reachable verification path
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: cause checkpoint recovery to restore metadata that diverges from certified replicated state
- Invariant to test: checkpoint and certification versions must not create cross-version state ambiguity
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
