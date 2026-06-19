# Q843: core protocol: next canonical encoding

## Question
Can an unprivileged attacker enter through a below-threshold protocol peer supplies validly framed but adversarial protocol data and drive `rs/nns/common/src/lib.rs`::next with attacker-controlled serialized request fields, timing, retries, message order, payload sizes, and cross-component state references to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this exploit missing binding between caller-controlled context and the state transition being applied, violating the invariant that publicly reachable edge cases must fail closed without bypassing consensus, accounting, or certification invariants, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/nns/common/src/lib.rs`::next
- Entrypoint: a below-threshold protocol peer supplies validly framed but adversarial protocol data
- Attacker controls: serialized request fields, timing, retries, message order, payload sizes, and cross-component state references
- Exploit idea: exploit missing binding between caller-controlled context and the state transition being applied
- Invariant to test: publicly reachable edge cases must fail closed without bypassing consensus, accounting, or certification invariants
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
