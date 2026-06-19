# Q501: crypto: From authorization boundary

## Question
Can an unprivileged attacker enter through a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs and drive `rs/crypto/src/sign/threshold_sig/ni_dkg/transcript/error_conversions.rs`::From with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/src/sign/threshold_sig/ni_dkg/transcript/error_conversions.rs`::From
- Entrypoint: a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
