# Q290: consensus: get payload signature/domain

## Question
Can an unprivileged attacker enter through an unprivileged ingress sender fills payload candidates that reach consensus validation and drive `rs/consensus/src/consensus/payload_builder.rs`::get_payload with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/src/consensus/payload_builder.rs`::get_payload
- Entrypoint: an unprivileged ingress sender fills payload candidates that reach consensus validation
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; mutate domain separators, registry versions, signer IDs, and message bytes independently
