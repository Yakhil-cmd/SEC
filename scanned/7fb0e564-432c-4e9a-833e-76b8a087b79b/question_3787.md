# Q3787: consensus: make setup initial dkg ids with height ordering/race

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer replays notarization/finalization/CUP artifacts and drive `rs/consensus/dkg/src/lib.rs`::make_setup_initial_dkg_ids_with_height with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/dkg/src/lib.rs`::make_setup_initial_dkg_ids_with_height
- Entrypoint: a below-threshold protocol peer replays notarization/finalization/CUP artifacts
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
