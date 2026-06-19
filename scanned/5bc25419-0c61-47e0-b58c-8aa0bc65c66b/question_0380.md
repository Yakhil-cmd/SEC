# Q380: crypto: idkg signature/domain

## Question
Can an unprivileged attacker enter through an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures and drive `rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/idkg.rs`::idkg with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this trigger parser ambiguity so two encodings verify as the same key or message in different layers, violating the invariant that malformed encodings and invalid curve/group elements must be rejected before key use, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/idkg.rs`::idkg
- Entrypoint: an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: trigger parser ambiguity so two encodings verify as the same key or message in different layers
- Invariant to test: malformed encodings and invalid curve/group elements must be rejected before key use
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; mutate domain separators, registry versions, signer IDs, and message bytes independently
