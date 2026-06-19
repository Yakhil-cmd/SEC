# Q3859: consensus: active transcripts certification/witness

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer replays notarization/finalization/CUP artifacts and drive `rs/consensus/idkg/src/complaints.rs`::active_transcripts with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/complaints.rs`::active_transcripts
- Entrypoint: a below-threshold protocol peer replays notarization/finalization/CUP artifacts
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
