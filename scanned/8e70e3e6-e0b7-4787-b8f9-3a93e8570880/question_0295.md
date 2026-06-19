# Q295: consensus: on state change cross module mismatch

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer replays notarization/finalization/CUP artifacts and drive `rs/consensus/src/consensus/share_aggregator.rs`::on_state_change with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/src/consensus/share_aggregator.rs`::on_state_change
- Entrypoint: a below-threshold protocol peer replays notarization/finalization/CUP artifacts
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
