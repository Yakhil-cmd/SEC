# Q3829: consensus: build callback id config map certification/witness

## Question
Can an unprivileged attacker enter through public call/ingress endpoint and drive `rs/consensus/dkg/src/remote.rs`::build_callback_id_config_map with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/dkg/src/remote.rs`::build_callback_id_config_map
- Entrypoint: public call/ingress endpoint
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
