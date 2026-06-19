# Q1013: consensus: panic on join err canonical encoding

## Question
Can an unprivileged attacker enter through a Byzantine-but-below-threshold peer gossips malformed consensus artifacts and drive `rs/p2p/consensus_manager/src/sender.rs`::panic_on_join_err with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/p2p/consensus_manager/src/sender.rs`::panic_on_join_err
- Entrypoint: a Byzantine-but-below-threshold peer gossips malformed consensus artifacts
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
