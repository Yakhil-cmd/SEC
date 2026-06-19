# Q3822: consensus: validate payload test case replay/idempotency

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/consensus/dkg/src/payload_validator.rs`::validate_payload_test_case with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/dkg/src/payload_validator.rs`::validate_payload_test_case
- Entrypoint: publicly reachable validation path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
