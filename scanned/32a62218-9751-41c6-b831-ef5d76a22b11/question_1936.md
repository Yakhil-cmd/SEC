# Q1936: consensus: remove all unvalidated below rollback edge case

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/artifact_pool/src/certification_pool.rs`::remove_all_unvalidated_below with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/certification_pool.rs`::remove_all_unvalidated_below
- Entrypoint: publicly reachable validation path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
