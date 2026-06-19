# Q2032: consensus: ecdsa signature shares by prefix replay/idempotency

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/artifact_pool/src/idkg_pool.rs`::ecdsa_signature_shares_by_prefix with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/idkg_pool.rs`::ecdsa_signature_shares_by_prefix
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
