# Q1685: crypto: parse signature cbor cross module mismatch

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `packages/ic-signature-verification/src/canister_sig.rs`::parse_signature_cbor with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `packages/ic-signature-verification/src/canister_sig.rs`::parse_signature_cbor
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
