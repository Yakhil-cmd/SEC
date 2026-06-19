# Q3924: consensus: make new pre signatures by priority resource accounting

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/consensus/idkg/src/payload_builder/pre_signatures.rs`::make_new_pre_signatures_by_priority with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to make fees, cycles, memory, instructions, or refunds diverge across success, reject, trap, and retry paths; specifically, can this race purge/admission logic so a stale but well-formed artifact influences finalization, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/payload_builder/pre_signatures.rs`::make_new_pre_signatures_by_priority
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: race purge/admission logic so a stale but well-formed artifact influences finalization
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results
