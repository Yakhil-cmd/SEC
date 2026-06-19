# Q841: core protocol: caller authorization boundary

## Question
Can an unprivileged attacker enter through public call/ingress endpoint and drive `rs/nns/common/src/access_control.rs`::caller with attacker-controlled serialized request fields, timing, retries, message order, payload sizes, and cross-component state references to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this force an edge-case error path to commit partial state or skip required cleanup, violating the invariant that producer/consumer modules must agree on authorization, canonical encoding, and state context, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/nns/common/src/access_control.rs`::caller
- Entrypoint: public call/ingress endpoint
- Attacker controls: serialized request fields, timing, retries, message order, payload sizes, and cross-component state references
- Exploit idea: force an edge-case error path to commit partial state or skip required cleanup
- Invariant to test: producer/consumer modules must agree on authorization, canonical encoding, and state context
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation
