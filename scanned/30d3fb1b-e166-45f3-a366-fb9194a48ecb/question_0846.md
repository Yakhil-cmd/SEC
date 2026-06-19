# Q846: core protocol: from u64 rollback edge case

## Question
Can an unprivileged attacker enter through a malicious canister triggers this module through message routing, execution, or management-canister flows and drive `rs/nns/common/src/types.rs`::from_u64 with attacker-controlled principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this force an edge-case error path to commit partial state or skip required cleanup, violating the invariant that errors and retries must not commit partial security-critical state, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/nns/common/src/types.rs`::from_u64
- Entrypoint: a malicious canister triggers this module through message routing, execution, or management-canister flows
- Attacker controls: principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads
- Exploit idea: force an edge-case error path to commit partial state or skip required cleanup
- Invariant to test: errors and retries must not commit partial security-critical state
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation
