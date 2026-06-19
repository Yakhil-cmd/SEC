# Q1163: state certification: insert chunk canonical encoding

## Question
Can an unprivileged attacker enter through a malicious subnet peer delays or reorders state-sync chunks around checkpoint creation and drive `rs/replicated_state/src/canister_state/canister_snapshots.rs`::insert_chunk with attacker-controlled checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this produce a witness that omits or aliases a security-critical subtree used by clients, violating the invariant that certified hash trees must be canonical and non-malleable for all attacker-chosen paths, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/replicated_state/src/canister_state/canister_snapshots.rs`::insert_chunk
- Entrypoint: a malicious subnet peer delays or reorders state-sync chunks around checkpoint creation
- Attacker controls: checkpoint heights, stream slices, state metadata, hash-tree labels, and certified-data keys
- Exploit idea: produce a witness that omits or aliases a security-critical subtree used by clients
- Invariant to test: certified hash trees must be canonical and non-malleable for all attacker-chosen paths
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
