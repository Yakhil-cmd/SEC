# Q2000: consensus: remove all signature/domain

## Question
Can an unprivileged attacker enter through a canister HTTP participant supplies divergent responses that enter consensus payload building and drive `rs/artifact_pool/src/height_index.rs`::remove_all with attacker-controlled committee shares, transcript references, block payload metadata, and artifact arrival order to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this trigger inconsistent handling of oversized or duplicated payload sections before notarization, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/height_index.rs`::remove_all
- Entrypoint: a canister HTTP participant supplies divergent responses that enter consensus payload building
- Attacker controls: committee shares, transcript references, block payload metadata, and artifact arrival order
- Exploit idea: trigger inconsistent handling of oversized or duplicated payload sections before notarization
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; mutate domain separators, registry versions, signer IDs, and message bytes independently
