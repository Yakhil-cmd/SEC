# Q682: core protocol: main replay/idempotency

## Question
Can an unprivileged attacker enter through a malicious canister triggers this module through message routing, execution, or management-canister flows and drive `rs/https_outcalls/adapter/src/main.rs`::main with attacker-controlled principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this exploit missing binding between caller-controlled context and the state transition being applied, violating the invariant that errors and retries must not commit partial security-critical state, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/https_outcalls/adapter/src/main.rs`::main
- Entrypoint: a malicious canister triggers this module through message routing, execution, or management-canister flows
- Attacker controls: principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads
- Exploit idea: exploit missing binding between caller-controlled context and the state transition being applied
- Invariant to test: errors and retries must not commit partial security-critical state
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
