# Q1992: consensus: validated contains replay/idempotency

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/artifact_pool/src/dkg_pool.rs`::validated_contains with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/dkg_pool.rs`::validated_contains
- Entrypoint: publicly reachable validation path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
