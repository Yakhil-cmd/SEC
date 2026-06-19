# Q3885: consensus: payload errors inc cross module mismatch

## Question
Can an unprivileged attacker enter through a Byzantine-but-below-threshold peer gossips malformed consensus artifacts and drive `rs/consensus/idkg/src/metrics.rs`::payload_errors_inc with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/metrics.rs`::payload_errors_inc
- Entrypoint: a Byzantine-but-below-threshold peer gossips malformed consensus artifacts
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
