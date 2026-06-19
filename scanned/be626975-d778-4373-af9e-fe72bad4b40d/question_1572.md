# Q1572: crypto: decrypt noauth replay/idempotency

## Question
Can an unprivileged attacker enter through an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures and drive `packages/ic-hpke/src/lib.rs`::decrypt_noauth with attacker-controlled DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that malformed encodings and invalid curve/group elements must be rejected before key use, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `packages/ic-hpke/src/lib.rs`::decrypt_noauth
- Entrypoint: an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures
- Attacker controls: DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: malformed encodings and invalid curve/group elements must be rejected before key use
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
