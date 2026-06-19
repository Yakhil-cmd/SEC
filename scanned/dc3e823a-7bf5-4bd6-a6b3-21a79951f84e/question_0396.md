# Q396: crypto: from fe rollback edge case

## Question
Can an unprivileged attacker enter through an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures and drive `rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/utils/group/secp256k1.rs`::from_fe with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that malformed encodings and invalid curve/group elements must be rejected before key use, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/utils/group/secp256k1.rs`::from_fe
- Entrypoint: an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: malformed encodings and invalid curve/group elements must be rejected before key use
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
