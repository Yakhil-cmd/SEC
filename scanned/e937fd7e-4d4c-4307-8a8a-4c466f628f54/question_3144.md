# Q3144: boundary: periodically poll api boundary nodes resource accounting

## Question
Can an unprivileged attacker enter through public call/ingress endpoint and drive `rs/boundary_node/rate_limits/canister/canister.rs`::periodically_poll_api_boundary_nodes with attacker-controlled Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering to make fees, cycles, memory, instructions, or refunds diverge across success, reject, trap, and retry paths; specifically, can this route a request to a different effective canister/subnet than the one validated or certified, violating the invariant that malformed public requests must fail closed without crashing in-scope platform components, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/rate_limits/canister/canister.rs`::periodically_poll_api_boundary_nodes
- Entrypoint: public call/ingress endpoint
- Attacker controls: Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering
- Exploit idea: route a request to a different effective canister/subnet than the one validated or certified
- Invariant to test: malformed public requests must fail closed without crashing in-scope platform components
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
