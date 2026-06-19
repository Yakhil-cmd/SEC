# Q60: consensus: set certification range signature/domain

## Question
Can an unprivileged attacker enter through certified-state/read_state path and drive `rs/artifact_pool/src/certification_pool.rs`::set_certification_range with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this cause block payload construction and validation to disagree about the same state or registry version, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/artifact_pool/src/certification_pool.rs`::set_certification_range
- Entrypoint: certified-state/read_state path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: cause block payload construction and validation to disagree about the same state or registry version
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; mutate domain separators, registry versions, signer IDs, and message bytes independently
