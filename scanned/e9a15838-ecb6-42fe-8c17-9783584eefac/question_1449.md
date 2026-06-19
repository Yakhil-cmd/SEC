# Q1449: crypto: Dkg Load Transcript Error certification/witness

## Question
Can an unprivileged attacker enter through a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs and drive `rs/types/types/src/crypto/threshold_sig/ni_dkg/errors/load_transcript_error.rs`::DkgLoadTranscriptError with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/types/types/src/crypto/threshold_sig/ni_dkg/errors/load_transcript_error.rs`::DkgLoadTranscriptError
- Entrypoint: a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
