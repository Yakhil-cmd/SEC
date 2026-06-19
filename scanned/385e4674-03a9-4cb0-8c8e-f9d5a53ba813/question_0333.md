# Q333: crypto: spki from components canonical encoding

## Question
Can an unprivileged attacker enter through a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs and drive `rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs`::spki_from_components with attacker-controlled DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this make verification accept a share/transcript under the wrong domain, registry version, or signer set, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/internal/crypto_lib/basic_sig/rsa_pkcs1/src/lib.rs`::spki_from_components
- Entrypoint: a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs
- Attacker controls: DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records
- Exploit idea: make verification accept a share/transcript under the wrong domain, registry version, or signer set
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
