# Q2956: boundary: is call rollback edge case

## Question
Can an unprivileged attacker enter through public call/ingress endpoint and drive `rs/boundary_node/ic_boundary/src/http/mod.rs`::is_call with attacker-controlled hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this trigger non-volumetric platform-level crash via a malformed but protocol-valid API request, violating the invariant that malformed public requests must fail closed without crashing in-scope platform components, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/http/mod.rs`::is_call
- Entrypoint: public call/ingress endpoint
- Attacker controls: hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing
- Exploit idea: trigger non-volumetric platform-level crash via a malformed but protocol-valid API request
- Invariant to test: malformed public requests must fail closed without crashing in-scope platform components
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
