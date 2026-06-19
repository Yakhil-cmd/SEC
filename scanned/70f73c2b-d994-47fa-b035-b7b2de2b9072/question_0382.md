# Q382: crypto: format curve id replay/idempotency

## Question
Can an unprivileged attacker enter through a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares and drive `rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/idkg/dealings.rs`::format_curve_id with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this make verification accept a share/transcript under the wrong domain, registry version, or signer set, violating the invariant that node/canister/TLS key validation must not accept keys outside the registered identity context, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/idkg/dealings.rs`::format_curve_id
- Entrypoint: a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: make verification accept a share/transcript under the wrong domain, registry version, or signer set
- Invariant to test: node/canister/TLS key validation must not accept keys outside the registered identity context
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
