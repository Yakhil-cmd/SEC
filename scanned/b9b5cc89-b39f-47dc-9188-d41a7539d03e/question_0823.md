# Q823: core protocol: acquire canonical encoding

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer supplies validly framed but adversarial protocol data and drive `rs/nervous_system/lock/src/lib.rs`::acquire with attacker-controlled principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this force an edge-case error path to commit partial state or skip required cleanup, violating the invariant that publicly reachable edge cases must fail closed without bypassing consensus, accounting, or certification invariants, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/nervous_system/lock/src/lib.rs`::acquire
- Entrypoint: a below-threshold protocol peer supplies validly framed but adversarial protocol data
- Attacker controls: principals, canister IDs, subnet IDs, registry versions, amounts, signatures, and encoded payloads
- Exploit idea: force an edge-case error path to commit partial state or skip required cleanup
- Invariant to test: publicly reachable edge cases must fail closed without bypassing consensus, accounting, or certification invariants
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
