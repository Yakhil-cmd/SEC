# Q1917: consensus: get unvalidated artifact ordering/race

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/artifact_pool/src/canister_http_pool.rs`::get_unvalidated_artifact with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/canister_http_pool.rs`::get_unvalidated_artifact
- Entrypoint: publicly reachable validation path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
