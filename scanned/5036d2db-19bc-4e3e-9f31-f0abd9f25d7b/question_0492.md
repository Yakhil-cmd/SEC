# Q492: crypto: sign basic replay/idempotency

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/crypto/src/sign/mod.rs`::sign_basic with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this make verification accept a share/transcript under the wrong domain, registry version, or signer set, violating the invariant that malformed encodings and invalid curve/group elements must be rejected before key use, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/src/sign/mod.rs`::sign_basic
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: make verification accept a share/transcript under the wrong domain, registry version, or signer set
- Invariant to test: malformed encodings and invalid curve/group elements must be rejected before key use
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
