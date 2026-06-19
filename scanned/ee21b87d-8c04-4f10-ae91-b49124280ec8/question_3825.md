# Q3825: consensus: target id cross module mismatch

## Question
Can an unprivileged attacker enter through a Byzantine-but-below-threshold peer gossips malformed consensus artifacts and drive `rs/consensus/dkg/src/remote.rs`::target_id with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/dkg/src/remote.rs`::target_id
- Entrypoint: a Byzantine-but-below-threshold peer gossips malformed consensus artifacts
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
