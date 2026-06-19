# Q2107: consensus: tx purge index below ordering/race

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer replays notarization/finalization/CUP artifacts and drive `rs/artifact_pool/src/lmdb_pool.rs`::tx_purge_index_below with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/lmdb_pool.rs`::tx_purge_index_below
- Entrypoint: a below-threshold protocol peer replays notarization/finalization/CUP artifacts
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
