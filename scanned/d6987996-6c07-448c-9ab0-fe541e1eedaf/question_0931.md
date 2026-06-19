# Q931: core protocol: build authorization boundary

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer supplies validly framed but adversarial protocol data and drive `rs/nns/handlers/root/impl/src/init.rs`::build with attacker-controlled state transition inputs, callbacks, certified paths, and malformed but parseable protocol data to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this force an edge-case error path to commit partial state or skip required cleanup, violating the invariant that publicly reachable edge cases must fail closed without bypassing consensus, accounting, or certification invariants, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/nns/handlers/root/impl/src/init.rs`::build
- Entrypoint: a below-threshold protocol peer supplies validly framed but adversarial protocol data
- Attacker controls: state transition inputs, callbacks, certified paths, and malformed but parseable protocol data
- Exploit idea: force an edge-case error path to commit partial state or skip required cleanup
- Invariant to test: publicly reachable edge cases must fail closed without bypassing consensus, accounting, or certification invariants
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation
