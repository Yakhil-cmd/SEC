# Q712: core protocol: or else replay/idempotency

## Question
Can an unprivileged attacker enter through a public API client races repeated requests through boundary, replica, and state-machine paths and drive `rs/interfaces/src/consensus_pool.rs`::or_else with attacker-controlled principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this exploit missing binding between caller-controlled context and the state transition being applied, violating the invariant that all attacker-controlled protocol inputs must be fully validated before state transition or certification, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/interfaces/src/consensus_pool.rs`::or_else
- Entrypoint: a public API client races repeated requests through boundary, replica, and state-machine paths
- Attacker controls: principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads
- Exploit idea: exploit missing binding between caller-controlled context and the state transition being applied
- Invariant to test: all attacker-controlled protocol inputs must be fully validated before state transition or certification
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
