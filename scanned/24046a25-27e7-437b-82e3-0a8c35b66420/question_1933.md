# Q1933: consensus: set certification share range canonical encoding

## Question
Can an unprivileged attacker enter through certified-state/read_state path and drive `rs/artifact_pool/src/certification_pool.rs`::set_certification_share_range with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that artifact acceptance must be independent of gossip ordering and local pool history, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/certification_pool.rs`::set_certification_share_range
- Entrypoint: certified-state/read_state path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: artifact acceptance must be independent of gossip ordering and local pool history
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
