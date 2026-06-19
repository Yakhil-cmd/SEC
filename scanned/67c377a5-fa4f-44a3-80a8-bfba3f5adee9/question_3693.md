# Q3693: state certification: verify certificate canonical encoding

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `rs/certification/src/lib.rs`::verify_certificate with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this cause checkpoint recovery to restore metadata that diverges from certified replicated state, violating the invariant that checkpoint and certification versions must not create cross-version state ambiguity, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/certification/src/lib.rs`::verify_certificate
- Entrypoint: publicly reachable verification path
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: cause checkpoint recovery to restore metadata that diverges from certified replicated state
- Invariant to test: checkpoint and certification versions must not create cross-version state ambiguity
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
