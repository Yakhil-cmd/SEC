# Q562: execution: set on cleanup replay/idempotency

## Question
Can an unprivileged attacker enter through an ingress sender installs or upgrades a crafted Wasm module through management-canister flows and drive `rs/embedders/src/wasmtime_embedder/system_api/request_in_prep.rs`::set_on_cleanup with attacker-controlled message queues, response payloads, instruction limits, heap growth, and rollback-triggering traps to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this exploit a DTS pause/resume boundary to reuse stale execution state or duplicate a response, violating the invariant that instruction, memory, and cycles accounting must remain conserved across retries and callbacks, and produce HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement?

## Target
- File/function: `rs/embedders/src/wasmtime_embedder/system_api/request_in_prep.rs`::set_on_cleanup
- Entrypoint: an ingress sender installs or upgrades a crafted Wasm module through management-canister flows
- Attacker controls: message queues, response payloads, instruction limits, heap growth, and rollback-triggering traps
- Exploit idea: exploit a DTS pause/resume boundary to reuse stale execution state or duplicate a response
- Invariant to test: instruction, memory, and cycles accounting must remain conserved across retries and callbacks
- Expected HackenProof impact: HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement
- Fast validation: run a state-machine test with crafted Wasm and assert state, cycles, callbacks, and certified data after trap/retry paths; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
