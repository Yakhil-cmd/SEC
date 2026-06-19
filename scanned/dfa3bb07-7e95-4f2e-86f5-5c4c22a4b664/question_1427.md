# Q1427: crypto: IDkg Transcript Id Error ordering/race

## Question
Can an unprivileged attacker enter through a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material and drive `rs/types/types/src/crypto/canister_threshold_sig/error.rs`::IDkgTranscriptIdError with attacker-controlled DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation, violating the invariant that cryptographic verification must bind message, domain, signer set, registry version, and transcript ID, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/types/types/src/crypto/canister_threshold_sig/error.rs`::IDkgTranscriptIdError
- Entrypoint: a boundary/API caller submits WebAuthn, canister-signature, TLS, or basic-signature material
- Attacker controls: DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records
- Exploit idea: bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation
- Invariant to test: cryptographic verification must bind message, domain, signer set, registry version, and transcript ID
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
