# Q432: crypto: update forward secure epoch replay/idempotency

## Question
Can an unprivileged attacker enter through an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures and drive `rs/crypto/internal/crypto_service_provider/src/threshold/ni_dkg/mod.rs`::update_forward_secure_epoch with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this trigger parser ambiguity so two encodings verify as the same key or message in different layers, violating the invariant that malformed encodings and invalid curve/group elements must be rejected before key use, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/internal/crypto_service_provider/src/threshold/ni_dkg/mod.rs`::update_forward_secure_epoch
- Entrypoint: an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: trigger parser ambiguity so two encodings verify as the same key or message in different layers
- Invariant to test: malformed encodings and invalid curve/group elements must be rejected before key use
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
