# Q1952: consensus: purge type below replay/idempotency

## Question
Can an unprivileged attacker enter through a canister HTTP participant supplies divergent responses that enter consensus payload building and drive `rs/artifact_pool/src/consensus_pool.rs`::purge_type_below with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/consensus_pool.rs`::purge_type_below
- Entrypoint: a canister HTTP participant supplies divergent responses that enter consensus payload building
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
