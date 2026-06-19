# Q1170: state certification: Try From signature/domain

## Question
Can an unprivileged attacker enter through a read_state/query caller requests certified data through crafted paths and witnesses and drive `rs/replicated_state/src/canister_state/queues/message_pool/proto.rs`::TryFrom with attacker-controlled chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this make two different states share an accepted manifest/hash-tree witness under edge-case labels, violating the invariant that state sync must only install authenticated chunks matching the certified manifest, and produce HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss?

## Target
- File/function: `rs/replicated_state/src/canister_state/queues/message_pool/proto.rs`::TryFrom
- Entrypoint: a read_state/query caller requests certified data through crafted paths and witnesses
- Attacker controls: chunk contents, manifest hashes, labeled-tree paths, certification versions, and witness requests
- Exploit idea: make two different states share an accepted manifest/hash-tree witness under edge-case labels
- Invariant to test: state sync must only install authenticated chunks matching the certified manifest
- Expected HackenProof impact: HackenProof Critical/High: forged certified state, invalid state installation, or replicated-state integrity loss
- Fast validation: build a manifest/witness differential test with malformed chunks or labels and assert hash/certification rejection; mutate domain separators, registry versions, signer IDs, and message bytes independently
