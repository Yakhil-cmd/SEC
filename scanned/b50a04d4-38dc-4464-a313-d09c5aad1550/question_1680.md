# Q1680: crypto: Shake256 signature/domain

## Question
Can an unprivileged attacker enter through an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures and drive `packages/ic-sha3/src/lib.rs`::Shake256 with attacker-controlled DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that malformed encodings and invalid curve/group elements must be rejected before key use, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `packages/ic-sha3/src/lib.rs`::Shake256
- Entrypoint: an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures
- Attacker controls: DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: malformed encodings and invalid curve/group elements must be rejected before key use
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; mutate domain separators, registry versions, signer IDs, and message bytes independently
