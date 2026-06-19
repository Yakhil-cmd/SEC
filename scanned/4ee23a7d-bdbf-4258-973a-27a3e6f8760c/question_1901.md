# Q1901: consensus: new with age func authorization boundary

## Question
Can an unprivileged attacker enter through a Byzantine-but-below-threshold peer gossips malformed consensus artifacts and drive `rs/artifact_pool/src/backup.rs`::new_with_age_func with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/backup.rs`::new_with_age_func
- Entrypoint: a Byzantine-but-below-threshold peer gossips malformed consensus artifacts
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
