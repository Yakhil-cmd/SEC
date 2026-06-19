# Q489: crypto: retrieve mega public key from registry certification/witness

## Question
Can an unprivileged attacker enter through public retrieve/withdraw/update-balance flow and drive `rs/crypto/src/sign/canister_threshold_sig/idkg/utils.rs`::retrieve_mega_public_key_from_registry with attacker-controlled DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/src/sign/canister_threshold_sig/idkg/utils.rs`::retrieve_mega_public_key_from_registry
- Entrypoint: public retrieve/withdraw/update-balance flow
- Attacker controls: DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records
- Exploit idea: bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
