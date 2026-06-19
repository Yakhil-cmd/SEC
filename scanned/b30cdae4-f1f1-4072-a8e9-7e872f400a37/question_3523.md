# Q3523: execution: Demux Server canonical encoding

## Question
Can an unprivileged attacker enter through a canister creates nested callbacks, rejects, replies, and DTS pauses that reach execution scheduling and drive `rs/canister_sandbox/src/sandbox_service.rs`::DemuxServer with attacker-controlled message queues, response payloads, instruction limits, heap growth, and rollback-triggering traps to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this make pre-trap accounting diverge from post-rollback state for cycles, memory, callbacks, or refunds, violating the invariant that canister lifecycle operations must not bypass controller/effective-canister authorization, and produce HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement?

## Target
- File/function: `rs/canister_sandbox/src/sandbox_service.rs`::DemuxServer
- Entrypoint: a canister creates nested callbacks, rejects, replies, and DTS pauses that reach execution scheduling
- Attacker controls: message queues, response payloads, instruction limits, heap growth, and rollback-triggering traps
- Exploit idea: make pre-trap accounting diverge from post-rollback state for cycles, memory, callbacks, or refunds
- Invariant to test: canister lifecycle operations must not bypass controller/effective-canister authorization
- Expected HackenProof impact: HackenProof High/Critical: canister integrity loss, unauthorized state mutation, or illegal cycles/funds movement
- Fast validation: run a state-machine test with crafted Wasm and assert state, cycles, callbacks, and certified data after trap/retry paths; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
