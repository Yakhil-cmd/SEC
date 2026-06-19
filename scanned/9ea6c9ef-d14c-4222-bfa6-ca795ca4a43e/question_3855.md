# Q3855: consensus: get openings for complaint cross module mismatch

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer replays notarization/finalization/CUP artifacts and drive `rs/consensus/idkg/src/complaints.rs`::get_openings_for_complaint with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/complaints.rs`::get_openings_for_complaint
- Entrypoint: a below-threshold protocol peer replays notarization/finalization/CUP artifacts
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
