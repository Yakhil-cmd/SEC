# Q443: crypto: pks and sks contains canonical encoding

## Question
Can an unprivileged attacker enter through a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material and drive `rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/public_and_secret_key_store/mod.rs`::pks_and_sks_contains with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this trigger parser ambiguity so two encodings verify as the same key or message in different layers, violating the invariant that cryptographic verification must bind message, domain, signer set, registry version, and transcript ID, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/public_and_secret_key_store/mod.rs`::pks_and_sks_contains
- Entrypoint: a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: trigger parser ambiguity so two encodings verify as the same key or message in different layers
- Invariant to test: cryptographic verification must bind message, domain, signer set, registry version, and transcript ID
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
