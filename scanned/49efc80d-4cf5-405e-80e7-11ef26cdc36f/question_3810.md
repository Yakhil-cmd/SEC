# Q3810: consensus: get unvalidated signature/domain

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/consensus/dkg/src/payload_builder.rs`::get_unvalidated with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/dkg/src/payload_builder.rs`::get_unvalidated
- Entrypoint: publicly reachable validation path
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: below-threshold peers must not break consensus safety, liveness, or finalized chain uniqueness
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; mutate domain separators, registry versions, signer IDs, and message bytes independently
