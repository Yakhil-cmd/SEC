# Q445: crypto: new public seed cross module mismatch

## Question
Can an unprivileged attacker enter through a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs and drive `rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/public_seed/mod.rs`::new_public_seed with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/public_seed/mod.rs`::new_public_seed
- Entrypoint: a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
