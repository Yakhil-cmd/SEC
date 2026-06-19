# Q681: core protocol: start server authorization boundary

## Question
Can an unprivileged attacker enter through an unprivileged ICP user submits ingress/query/read_state inputs that reach this module transitively and drive `rs/https_outcalls/adapter/src/lib.rs`::start_server with attacker-controlled state transition inputs, callbacks, certified paths, and malformed but parseable protocol data to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this make this module accept state that a downstream in-scope component treats as already validated, violating the invariant that producer/consumer modules must agree on authorization, canonical encoding, and state context, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/https_outcalls/adapter/src/lib.rs`::start_server
- Entrypoint: an unprivileged ICP user submits ingress/query/read_state inputs that reach this module transitively
- Attacker controls: state transition inputs, callbacks, certified paths, and malformed but parseable protocol data
- Exploit idea: make this module accept state that a downstream in-scope component treats as already validated
- Invariant to test: producer/consumer modules must agree on authorization, canonical encoding, and state context
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation
