# Q2049: consensus: insert validated artifact with timestamps certification/witness

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/artifact_pool/src/ingress_pool.rs`::insert_validated_artifact_with_timestamps with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/ingress_pool.rs`::insert_validated_artifact_with_timestamps
- Entrypoint: publicly reachable validation path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
