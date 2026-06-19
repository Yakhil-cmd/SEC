# Q3786: consensus: assert no remote transcript duplicates rollback edge case

## Question
Can an unprivileged attacker enter through an unprivileged ingress sender fills payload candidates that reach consensus validation and drive `rs/consensus/dkg/src/lib.rs`::assert_no_remote_transcript_duplicates with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/dkg/src/lib.rs`::assert_no_remote_transcript_duplicates
- Entrypoint: an unprivileged ingress sender fills payload candidates that reach consensus validation
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
