# Q3469: execution: Demux Server certification/witness

## Question
Can an unprivileged attacker enter through a malicious canister invokes public system API calls during update/query execution and drive `rs/canister_sandbox/src/rpc.rs`::DemuxServer with attacker-controlled message queues, response payloads, instruction limits, heap growth, and rollback-triggering traps to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this cross a sandbox/system-api boundary with malformed memory references or encoded messages, violating the invariant that system API and sandbox IPC must not allow memory isolation or authorization bypass, and produce HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement?

## Target
- File/function: `rs/canister_sandbox/src/rpc.rs`::DemuxServer
- Entrypoint: a malicious canister invokes public system API calls during update/query execution
- Attacker controls: message queues, response payloads, instruction limits, heap growth, and rollback-triggering traps
- Exploit idea: cross a sandbox/system-api boundary with malformed memory references or encoded messages
- Invariant to test: system API and sandbox IPC must not allow memory isolation or authorization bypass
- Expected HackenProof impact: HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement
- Fast validation: run a state-machine test with crafted Wasm and assert state, cycles, callbacks, and certified data after trap/retry paths
