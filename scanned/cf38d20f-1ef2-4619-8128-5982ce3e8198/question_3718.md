# Q3718: consensus: to unvalidated bounds/overflow

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/consensus/certification/src/certifier.rs`::to_unvalidated with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/certification/src/certifier.rs`::to_unvalidated
- Entrypoint: publicly reachable validation path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
