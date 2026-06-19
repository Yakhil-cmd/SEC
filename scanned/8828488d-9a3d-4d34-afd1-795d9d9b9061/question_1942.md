# Q1942: consensus: certifications replay/idempotency

## Question
Can an unprivileged attacker enter through certified-state/read_state path and drive `rs/artifact_pool/src/certification_pool.rs`::certifications with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/certification_pool.rs`::certifications
- Entrypoint: certified-state/read_state path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
