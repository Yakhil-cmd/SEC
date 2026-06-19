# Q1677: crypto: finalize xof ordering/race

## Question
Can an unprivileged attacker enter through a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs and drive `packages/ic-sha3/src/lib.rs`::finalize_xof with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `packages/ic-sha3/src/lib.rs`::finalize_xof
- Entrypoint: a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
