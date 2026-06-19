# Q798: core protocol: canister metadata bounds/overflow

## Question
Can an unprivileged attacker enter through a malicious canister triggers this module through message routing, execution, or management-canister flows and drive `rs/nervous_system/clients/src/canister_metadata.rs`::canister_metadata with attacker-controlled principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this force an edge-case error path to commit partial state or skip required cleanup, violating the invariant that errors and retries must not commit partial security-critical state, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/nervous_system/clients/src/canister_metadata.rs`::canister_metadata
- Entrypoint: a malicious canister triggers this module through message routing, execution, or management-canister flows
- Attacker controls: principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads
- Exploit idea: force an edge-case error path to commit partial state or skip required cleanup
- Invariant to test: errors and retries must not commit partial security-critical state
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
