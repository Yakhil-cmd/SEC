# Q2088: consensus: Into Inner bounds/overflow

## Question
Can an unprivileged attacker enter through a canister HTTP participant supplies divergent responses that enter consensus payload building and drive `rs/artifact_pool/src/lib.rs`::IntoInner with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/lib.rs`::IntoInner
- Entrypoint: a canister HTTP participant supplies divergent responses that enter consensus payload building
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
