# Q3976: consensus: purge artifacts rollback edge case

## Question
Can an unprivileged attacker enter through a canister HTTP participant supplies divergent responses that enter consensus payload building and drive `rs/consensus/idkg/src/signer.rs`::purge_artifacts with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/signer.rs`::purge_artifacts
- Entrypoint: a canister HTTP participant supplies divergent responses that enter consensus payload building
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
