# Q491: crypto: get tschnorr master public key from internal transcript authorization boundary

## Question
Can an unprivileged attacker enter through a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material and drive `rs/crypto/src/sign/canister_threshold_sig/schnorr.rs`::get_tschnorr_master_public_key_from_internal_transcript with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that cryptographic verification must bind message, domain, signer set, registry version, and transcript ID, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/src/sign/canister_threshold_sig/schnorr.rs`::get_tschnorr_master_public_key_from_internal_transcript
- Entrypoint: a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: cryptographic verification must bind message, domain, signer set, registry version, and transcript ID
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
