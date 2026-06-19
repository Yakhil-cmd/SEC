# Q3700: state certification: parse certificate signature/domain

## Question
Can an unprivileged attacker enter through certified-state/read_state path and drive `rs/certification/src/lib.rs`::parse_certificate with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this produce a witness that omits or aliases a security-critical subtree used by clients, violating the invariant that read_state witnesses must not prove stale, missing, or forged subnet/canister state, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/certification/src/lib.rs`::parse_certificate
- Entrypoint: certified-state/read_state path
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: produce a witness that omits or aliases a security-critical subtree used by clients
- Invariant to test: read_state witnesses must not prove stale, missing, or forged subnet/canister state
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; mutate domain separators, registry versions, signer IDs, and message bytes independently
