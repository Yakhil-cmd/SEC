# Q932: core protocol: now nanoseconds replay/idempotency

## Question
Can an unprivileged attacker enter through a public API client races repeated requests through boundary, replica, and state-machine paths and drive `rs/nns/handlers/root/impl/src/lib.rs`::now_nanoseconds with attacker-controlled principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this force an edge-case error path to commit partial state or skip required cleanup, violating the invariant that all attacker-controlled protocol inputs must be fully validated before state transition or certification, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/nns/handlers/root/impl/src/lib.rs`::now_nanoseconds
- Entrypoint: a public API client races repeated requests through boundary, replica, and state-machine paths
- Attacker controls: principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads
- Exploit idea: force an edge-case error path to commit partial state or skip required cleanup
- Invariant to test: all attacker-controlled protocol inputs must be fully validated before state transition or certification
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
