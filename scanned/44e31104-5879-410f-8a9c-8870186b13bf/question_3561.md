# Q3561: state certification: Request V19 authorization boundary

## Question
Can an unprivileged attacker enter through a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata and drive `rs/canonical_state/src/encoding/old_types.rs`::RequestV19 with attacker-controlled chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this make two different states share an accepted manifest/hash-tree witness under edge-case labels, violating the invariant that checkpoint and certification versions must not create cross-version state ambiguity, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/canonical_state/src/encoding/old_types.rs`::RequestV19
- Entrypoint: a protocol peer offers malformed state-sync chunks, manifests, or checkpoint metadata
- Attacker controls: chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests
- Exploit idea: make two different states share an accepted manifest/hash-tree witness under edge-case labels
- Invariant to test: checkpoint and certification versions must not create cross-version state ambiguity
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection
