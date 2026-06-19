# Q1990: consensus: get unvalidated signature/domain

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/artifact_pool/src/dkg_pool.rs`::get_unvalidated with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/dkg_pool.rs`::get_unvalidated
- Entrypoint: publicly reachable validation path
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; mutate domain separators, registry versions, signer IDs, and message bytes independently
