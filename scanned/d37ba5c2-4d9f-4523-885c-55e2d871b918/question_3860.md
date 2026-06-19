# Q3860: consensus: get active transcripts signature/domain

## Question
Can an unprivileged attacker enter through a canister HTTP participant supplies divergent responses that enter consensus payload building and drive `rs/consensus/idkg/src/lib.rs`::get_active_transcripts with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/lib.rs`::get_active_transcripts
- Entrypoint: a canister HTTP participant supplies divergent responses that enter consensus payload building
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; mutate domain separators, registry versions, signer IDs, and message bytes independently
