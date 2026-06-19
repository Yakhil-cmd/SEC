# Q3896: consensus: make bootstrap summary with initial dealings rollback edge case

## Question
Can an unprivileged attacker enter through a canister HTTP participant supplies divergent responses that enter consensus payload building and drive `rs/consensus/idkg/src/payload_builder.rs`::make_bootstrap_summary_with_initial_dealings with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/payload_builder.rs`::make_bootstrap_summary_with_initial_dealings
- Entrypoint: a canister HTTP participant supplies divergent responses that enter consensus payload building
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
