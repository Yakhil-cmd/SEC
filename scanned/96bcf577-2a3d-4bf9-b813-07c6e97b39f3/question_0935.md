# Q935: core protocol: change canister controllers cross module mismatch

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer supplies validly framed but adversarial protocol data and drive `rs/nns/handlers/root/interface/src/client.rs`::change_canister_controllers with attacker-controlled principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads to make producer and consumer modules disagree on height, registry version, state hash, or authorization context; specifically, can this exploit missing binding between caller-controlled context and the state transition being applied, violating the invariant that publicly reachable edge cases must fail closed without bypassing consensus, accounting, or certification invariants, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/nns/handlers/root/interface/src/client.rs`::change_canister_controllers
- Entrypoint: a below-threshold protocol peer supplies validly framed but adversarial protocol data
- Attacker controls: principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads
- Exploit idea: exploit missing binding between caller-controlled context and the state transition being applied
- Invariant to test: publicly reachable edge cases must fail closed without bypassing consensus, accounting, or certification invariants
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation
