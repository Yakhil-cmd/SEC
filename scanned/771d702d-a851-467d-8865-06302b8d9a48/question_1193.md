# Q1193: state certification: subnets canonical encoding

## Question
Can an unprivileged attacker enter through a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata and drive `rs/replicated_state/src/metadata_state.rs`::subnets with attacker-controlled partial manifests, duplicate chunks, tree traversal order, and read_state path shape to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this accept a state-sync chunk whose content is not bound to the manifest and height being certified, violating the invariant that checkpoint and certification versions must not create cross-version state ambiguity, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/replicated_state/src/metadata_state.rs`::subnets
- Entrypoint: a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata
- Attacker controls: partial manifests, duplicate chunks, tree traversal order, and read_state path shape
- Exploit idea: accept a state-sync chunk whose content is not bound to the manifest and height being certified
- Invariant to test: checkpoint and certification versions must not create cross-version state ambiguity
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
