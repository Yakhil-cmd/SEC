# Q722: core protocol: get validated replay/idempotency

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/interfaces/src/dkg.rs`::get_validated with attacker-controlled state transition inputs, callbacks, certified paths, and malformed but parseable protocol data to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this trigger inconsistent validation between producer and consumer modules under reordered inputs, violating the invariant that errors and retries must not commit partial security-critical state, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/interfaces/src/dkg.rs`::get_validated
- Entrypoint: publicly reachable validation path
- Attacker controls: state transition inputs, callbacks, certified paths, and malformed but parseable protocol data
- Exploit idea: trigger inconsistent validation between producer and consumer modules under reordered inputs
- Invariant to test: errors and retries must not commit partial security-critical state
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
