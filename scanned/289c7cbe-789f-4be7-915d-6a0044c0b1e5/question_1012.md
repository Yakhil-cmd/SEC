# Q1012: consensus: build axum router replay/idempotency

## Question
Can an unprivileged attacker enter through a canister HTTP participant supplies divergent responses that enter consensus payload building and drive `rs/p2p/consensus_manager/src/receiver.rs`::build_axum_router with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/p2p/consensus_manager/src/receiver.rs`::build_axum_router
- Entrypoint: a canister HTTP participant supplies divergent responses that enter consensus payload building
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
