# Q728: core protocol: lib bounds/overflow

## Question
Can an unprivileged attacker enter through a public API client races repeated requests through boundary, replica, and state-machine paths and drive `rs/interfaces/src/lib.rs`::lib with attacker-controlled principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this force an edge-case error path to commit partial state or skip required cleanup, violating the invariant that all attacker-controlled protocol inputs must be fully validated before state transition or certification, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/interfaces/src/lib.rs`::lib
- Entrypoint: a public API client races repeated requests through boundary, replica, and state-machine paths
- Attacker controls: principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads
- Exploit idea: force an edge-case error path to commit partial state or skip required cleanup
- Invariant to test: all attacker-controlled protocol inputs must be fully validated before state transition or certification
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
