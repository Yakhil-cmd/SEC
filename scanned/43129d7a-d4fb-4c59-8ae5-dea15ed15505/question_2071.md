# Q2071: consensus: get by height authorization boundary

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer replays notarization/finalization/CUP artifacts and drive `rs/artifact_pool/src/inmemory_pool.rs`::get_by_height with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/inmemory_pool.rs`::get_by_height
- Entrypoint: a below-threshold protocol peer replays notarization/finalization/CUP artifacts
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
