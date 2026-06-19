# Q495: crypto: create dealing cross module mismatch

## Question
Can an unprivileged attacker enter through a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material and drive `rs/crypto/src/sign/threshold_sig/ni_dkg.rs`::create_dealing with attacker-controlled DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this make verification accept a share/transcript under the wrong domain, registry version, or signer set, violating the invariant that cryptographic verification must bind message, domain, signer set, registry version, and transcript ID, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/src/sign/threshold_sig/ni_dkg.rs`::create_dealing
- Entrypoint: a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material
- Attacker controls: DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records
- Exploit idea: make verification accept a share/transcript under the wrong domain, registry version, or signer set
- Invariant to test: cryptographic verification must bind message, domain, signer set, registry version, and transcript ID
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
