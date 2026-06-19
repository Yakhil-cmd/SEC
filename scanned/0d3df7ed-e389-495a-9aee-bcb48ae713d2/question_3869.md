# Q3869: consensus: IDkg Bouncer certification/witness

## Question
Can an unprivileged attacker enter through a Byzantine-but-below-threshold peer gossips malformed consensus artifacts and drive `rs/consensus/idkg/src/lib.rs`::IDkgBouncer with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/lib.rs`::IDkgBouncer
- Entrypoint: a Byzantine-but-below-threshold peer gossips malformed consensus artifacts
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
