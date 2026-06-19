# Q2100: consensus: update meta signature/domain

## Question
Can an unprivileged attacker enter through a canister HTTP participant supplies divergent responses that enter consensus payload building and drive `rs/artifact_pool/src/lmdb_pool.rs`::update_meta with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/lmdb_pool.rs`::update_meta
- Entrypoint: a canister HTTP participant supplies divergent responses that enter consensus payload building
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; mutate domain separators, registry versions, signer IDs, and message bytes independently
