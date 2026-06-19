# Q3948: consensus: should not verify same transcript many times bounds/overflow

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `rs/consensus/idkg/src/payload_verifier.rs`::should_not_verify_same_transcript_many_times with attacker-controlled ingress batches, payload limits, expiry windows, and repeated admissible messages to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this make validation accept an artifact whose dependencies or height context differ across honest replicas, violating the invariant that honest replicas must not finalize or notarize a block that fails deterministic validation, and produce HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization?

## Target
- File/function: `rs/consensus/idkg/src/payload_verifier.rs`::should_not_verify_same_transcript_many_times
- Entrypoint: publicly reachable verification path
- Attacker controls: ingress batches, payload limits, expiry windows, and repeated admissible messages
- Exploit idea: make validation accept an artifact whose dependencies or height context differ across honest replicas
- Invariant to test: honest replicas must not finalize or notarize a block that fails deterministic validation
- Expected HackenProof impact: HackenProof Critical: consensus integrity compromise or arbitrary invalid block insertion/finalization
- Fast validation: construct a local consensus/pool test with two artifact arrival orders and assert identical validation/finalization results; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
