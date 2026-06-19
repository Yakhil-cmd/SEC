# Q1429: crypto: From certification/witness

## Question
Can an unprivileged attacker enter through a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs and drive `rs/types/types/src/crypto/canister_threshold_sig/idkg/conversions.rs`::From with attacker-controlled DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this make verification accept a share/transcript under the wrong domain, registry version, or signer set, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/types/types/src/crypto/canister_threshold_sig/idkg/conversions.rs`::From
- Entrypoint: a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs
- Attacker controls: DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records
- Exploit idea: make verification accept a share/transcript under the wrong domain, registry version, or signer set
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
