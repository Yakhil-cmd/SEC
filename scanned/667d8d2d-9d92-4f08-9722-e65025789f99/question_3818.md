# Q3818: consensus: validate dealings payload when existing dealer fails test bounds/overflow

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/consensus/dkg/src/payload_validator.rs`::validate_dealings_payload_when_existing_dealer_fails_test with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/dkg/src/payload_validator.rs`::validate_dealings_payload_when_existing_dealer_fails_test
- Entrypoint: publicly reachable validation path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
