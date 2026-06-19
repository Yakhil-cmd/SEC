# Q936: core protocol: error rollback edge case

## Question
Can an unprivileged attacker enter through a public API client races repeated requests through boundary, replica, and state-machine paths and drive `rs/nns/handlers/root/interface/src/lib.rs`::error with attacker-controlled principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this make this module accept state that a downstream in-scope component treats as already validated, violating the invariant that all attacker-controlled protocol inputs must be fully validated before state transition or certification, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/nns/handlers/root/interface/src/lib.rs`::error
- Entrypoint: a public API client races repeated requests through boundary, replica, and state-machine paths
- Attacker controls: principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads
- Exploit idea: make this module accept state that a downstream in-scope component treats as already validated
- Invariant to test: all attacker-controlled protocol inputs must be fully validated before state transition or certification
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation
