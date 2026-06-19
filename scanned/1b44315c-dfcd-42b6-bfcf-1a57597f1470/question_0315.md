# Q315: crypto: signing canister id cross module mismatch

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/crypto/iccsa/src/types.rs`::signing_canister_id with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that cryptographic verification must bind message, domain, signer set, registry version, and transcript ID, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/iccsa/src/types.rs`::signing_canister_id
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: cryptographic verification must bind message, domain, signer set, registry version, and transcript ID
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
