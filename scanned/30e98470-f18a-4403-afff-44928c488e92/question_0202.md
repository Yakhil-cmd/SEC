# Q202: execution: execution finished replay/idempotency

## Question
Can an unprivileged attacker enter through an ingress sender installs or upgrades a crafted Wasm module through management-canister flows and drive `rs/canister_sandbox/src/controller_service.rs`::execution_finished with attacker-controlled Wasm bytecode, system API arguments, stable-memory offsets, cycles, callbacks, and reject paths to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this cross a sandbox/system-api boundary with malformed memory references or encoded messages, violating the invariant that instruction, memory, and cycles accounting must remain conserved across retries and callbacks, and produce HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement?

## Target
- File/function: `rs/canister_sandbox/src/controller_service.rs`::execution_finished
- Entrypoint: an ingress sender installs or upgrades a crafted Wasm module through management-canister flows
- Attacker controls: Wasm bytecode, system API arguments, stable-memory offsets, cycles, callbacks, and reject paths
- Exploit idea: cross a sandbox/system-api boundary with malformed memory references or encoded messages
- Invariant to test: instruction, memory, and cycles accounting must remain conserved across retries and callbacks
- Expected HackenProof impact: HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement
- Fast validation: run a state-machine test with crafted Wasm and assert state, cycles, callbacks, and certified data after trap/retry paths; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
