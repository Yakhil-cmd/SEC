# Q17: crypto: verify canister sig ordering/race

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `packages/ic-signature-verification/src/canister_sig.rs`::verify_canister_sig with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `packages/ic-signature-verification/src/canister_sig.rs`::verify_canister_sig
- Entrypoint: publicly reachable verification path
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
