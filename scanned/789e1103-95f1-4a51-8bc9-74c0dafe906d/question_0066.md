# Q66: consensus: insert rollback edge case

## Question
Can an unprivileged attacker enter through an unprivileged ingress sender fills payload candidates that reach consensus validation and drive `rs/artifact_pool/src/ingress_pool.rs`::insert with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/ingress_pool.rs`::insert
- Entrypoint: an unprivileged ingress sender fills payload candidates that reach consensus validation
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
