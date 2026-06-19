# Q650: execution: update signature request contexts signature/domain

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `rs/execution_environment/src/scheduler/threshold_signatures.rs`::update_signature_request_contexts with attacker-controlled install/upgrade payloads, canister settings, controllers, memory pages, and execution round timing to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this cause query and replicated execution to disagree about certified or observable state, violating the invariant that instruction, memory, and cycles accounting must remain conserved across retries and callbacks, and produce HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement?

## Target
- File/function: `rs/execution_environment/src/scheduler/threshold_signatures.rs`::update_signature_request_contexts
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: install/upgrade payloads, canister settings, controllers, memory pages, and execution round timing
- Exploit idea: cause query and replicated execution to disagree about certified or observable state
- Invariant to test: instruction, memory, and cycles accounting must remain conserved across retries and callbacks
- Expected HackenProof impact: HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement
- Fast validation: run a state-machine test with crafted Wasm and assert state, cycles, callbacks, and certified data after trap/retry paths; mutate domain separators, registry versions, signer IDs, and message bytes independently
