# Q2027: consensus: signed dealings by transcript id ordering/race

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/artifact_pool/src/idkg_pool.rs`::signed_dealings_by_transcript_id with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/idkg_pool.rs`::signed_dealings_by_transcript_id
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: payload validation must bind ingress, xnet, canister HTTP, and chain-key sections to the same context
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
