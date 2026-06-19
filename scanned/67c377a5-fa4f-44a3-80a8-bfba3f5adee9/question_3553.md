# Q3553: state certification: encode subnet canister ranges canonical encoding

## Question
Can an unprivileged attacker enter through a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata and drive `rs/canonical_state/src/encoding.rs`::encode_subnet_canister_ranges with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this produce a witness that omits or aliases a security-critical subtree used by clients, violating the invariant that checkpoint and certification versions must not create cross-version state ambiguity, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/encoding.rs`::encode_subnet_canister_ranges
- Entrypoint: a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: produce a witness that omits or aliases a security-critical subtree used by clients
- Invariant to test: checkpoint and certification versions must not create cross-version state ambiguity
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
