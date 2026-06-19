# Q3027: boundary: dummy call ordering/race

## Question
Can an unprivileged attacker enter through public call/ingress endpoint and drive `rs/boundary_node/ic_boundary/src/rate_limiting/mod.rs`::dummy_call with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this trigger non-volumetric platform-level crash via a malformed but protocol-valid API request, violating the invariant that rate limiting and routing must not bypass ingress validation or expose privileged operations, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/rate_limiting/mod.rs`::dummy_call
- Entrypoint: public call/ingress endpoint
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: trigger non-volumetric platform-level crash via a malformed but protocol-valid API request
- Invariant to test: rate limiting and routing must not bypass ingress validation or expose privileged operations
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
