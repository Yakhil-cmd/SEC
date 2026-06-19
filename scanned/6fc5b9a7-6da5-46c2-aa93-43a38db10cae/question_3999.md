# Q3999: consensus: IDkg Stats Internal certification/witness

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer replays notarization/finalization/CUP artifacts and drive `rs/consensus/idkg/src/stats.rs`::IDkgStatsInternal with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/stats.rs`::IDkgStatsInternal
- Entrypoint: a below-threshold protocol peer replays notarization/finalization/CUP artifacts
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
