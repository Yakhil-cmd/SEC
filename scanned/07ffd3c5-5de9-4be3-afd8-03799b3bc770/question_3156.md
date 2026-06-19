# Q3156: boundary: for rollback edge case

## Question
Can an unprivileged attacker enter through an unprivileged user sends malformed signatures, delegations, principals, or WebAuthn credentials to ingress validation and drive `rs/boundary_node/rate_limits/canister/getter.rs`::for with attacker-controlled Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch, violating the invariant that malformed public requests must fail closed without crashing in-scope platform components, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/rate_limits/canister/getter.rs`::for
- Entrypoint: an unprivileged user sends malformed signatures, delegations, principals, or WebAuthn credentials to ingress validation
- Attacker controls: Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering
- Exploit idea: bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch
- Invariant to test: malformed public requests must fail closed without crashing in-scope platform components
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
