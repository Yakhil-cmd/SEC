# Q201: execution: sandbox exited authorization boundary

## Question
Can an unprivileged attacker enter through a malicious canister invokes public system API calls during update/query execution and drive `rs/canister_sandbox/src/controller_launcher_service.rs`::sandbox_exited with attacker-controlled install/upgrade payloads, canister settings, controllers, memory pages, and execution round timing to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this cross a sandbox/system-api boundary with malformed memory references or encoded messages, violating the invariant that system API and sandbox IPC must not allow memory isolation or authorization bypass, and produce HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement?

## Target
- File/function: `rs/canister_sandbox/src/controller_launcher_service.rs`::sandbox_exited
- Entrypoint: a malicious canister invokes public system API calls during update/query execution
- Attacker controls: install/upgrade payloads, canister settings, controllers, memory pages, and execution round timing
- Exploit idea: cross a sandbox/system-api boundary with malformed memory references or encoded messages
- Invariant to test: system API and sandbox IPC must not allow memory isolation or authorization bypass
- Expected HackenProof impact: HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement
- Fast validation: run a state-machine test with crafted Wasm and assert state, cycles, callbacks, and certified data after trap/retry paths
