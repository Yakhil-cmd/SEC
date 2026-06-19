# Q3920: consensus: count pre signatures total signature/domain

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/consensus/idkg/src/payload_builder/pre_signatures.rs`::count_pre_signatures_total with attacker-controlled artifact bytes, heights, registry versions, validation context, and timing/order of gossip to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/payload_builder/pre_signatures.rs`::count_pre_signatures_total
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: artifact bytes, heights, registry versions, validation context, and timing/order of gossip
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; mutate domain separators, registry versions, signer IDs, and message bytes independently
