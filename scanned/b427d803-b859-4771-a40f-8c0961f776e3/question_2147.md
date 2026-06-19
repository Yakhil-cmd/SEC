# Q2147: consensus: remove block payload ordering/race

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer replays notarization/finalization/CUP artifacts and drive `rs/artifact_pool/src/rocksdb_pool.rs`::remove_block_payload with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/rocksdb_pool.rs`::remove_block_payload
- Entrypoint: a below-threshold protocol peer replays notarization/finalization/CUP artifacts
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
