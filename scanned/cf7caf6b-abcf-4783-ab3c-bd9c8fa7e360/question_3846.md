# Q3846: consensus: validate openings rollback edge case

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/consensus/idkg/src/complaints.rs`::validate_openings with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/complaints.rs`::validate_openings
- Entrypoint: publicly reachable validation path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
