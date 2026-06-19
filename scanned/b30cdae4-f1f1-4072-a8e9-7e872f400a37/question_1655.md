# Q1655: crypto: deserialize sec1 cross module mismatch

## Question
Can an unprivileged attacker enter through a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material and drive `packages/ic-secp256k1/src/lib.rs`::deserialize_sec1 with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that cryptographic verification must bind message, domain, signer set, registry version, and transcript ID, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `packages/ic-secp256k1/src/lib.rs`::deserialize_sec1
- Entrypoint: a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: cryptographic verification must bind message, domain, signer set, registry version, and transcript ID
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
