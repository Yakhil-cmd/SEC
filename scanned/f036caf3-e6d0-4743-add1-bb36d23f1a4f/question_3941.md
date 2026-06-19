# Q3941: consensus: validate summary payload authorization boundary

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/consensus/idkg/src/payload_verifier.rs`::validate_summary_payload with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/payload_verifier.rs`::validate_summary_payload
- Entrypoint: publicly reachable validation path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
