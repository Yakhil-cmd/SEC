# Q680: core protocol: Incoming Source signature/domain

## Question
Can an unprivileged attacker enter through a public API client races repeated requests through boundary, replica, and state-machine paths and drive `rs/https_outcalls/adapter/src/config.rs`::IncomingSource with attacker-controlled serialized request fields, timing, retries, message order, payload sizes, and cross-component state references to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this exploit missing binding between caller-controlled context and the state transition being applied, violating the invariant that all attacker-controlled protocol inputs must be fully validated before state transition or certification, and produce HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact?

## Target
- File/function: `rs/https_outcalls/adapter/src/config.rs`::IncomingSource
- Entrypoint: a public API client races repeated requests through boundary, replica, and state-machine paths
- Attacker controls: serialized request fields, timing, retries, message order, payload sizes, and cross-component state references
- Exploit idea: exploit missing binding between caller-controlled context and the state transition being applied
- Invariant to test: all attacker-controlled protocol inputs must be fully validated before state transition or certification
- Expected HackenProof impact: HackenProof High/Medium: production protocol integrity, authorization, accounting, certification, or platform-availability impact
- Fast validation: add a focused unit/state-machine/fuzz test around the module boundary and assert rejection or invariant preservation; mutate domain separators, registry versions, signer IDs, and message bytes independently
