# Q1949: consensus: unvalidated certifications at height certification/witness

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/artifact_pool/src/certification_pool.rs`::unvalidated_certifications_at_height with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/certification_pool.rs`::unvalidated_certifications_at_height
- Entrypoint: publicly reachable validation path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
