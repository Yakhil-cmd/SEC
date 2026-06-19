# Q1643: crypto: is empty canonical encoding

## Question
Can an unprivileged attacker enter through a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material and drive `packages/ic-secp256k1/src/lib.rs`::is_empty with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that cryptographic verification must bind message, domain, signer set, registry version, and transcript ID, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `packages/ic-secp256k1/src/lib.rs`::is_empty
- Entrypoint: a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: cryptographic verification must bind message, domain, signer set, registry version, and transcript ID
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
