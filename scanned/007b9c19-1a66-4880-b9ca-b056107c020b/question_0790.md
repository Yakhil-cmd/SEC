# Q790: core protocol: execute round signature/domain

## Question
Can an unprivileged attacker enter through a malicious canister triggers this module through message routing, execution, or management-canister flows and drive `rs/messaging/src/state_machine.rs`::execute_round with attacker-controlled principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this exploit missing binding between caller-controlled context and the state transition being applied, violating the invariant that errors and retries must not commit partial security-critical state, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/messaging/src/state_machine.rs`::execute_round
- Entrypoint: a malicious canister triggers this module through message routing, execution, or management-canister flows
- Attacker controls: principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads
- Exploit idea: exploit missing binding between caller-controlled context and the state transition being applied
- Invariant to test: errors and retries must not commit partial security-critical state
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation; mutate domain separators, registry versions, signer IDs, and message bytes independently
