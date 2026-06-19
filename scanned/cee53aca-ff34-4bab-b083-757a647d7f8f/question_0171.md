# Q171: boundary: verify server cert authorization boundary

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `rs/boundary_node/ic_boundary/src/tls_verify.rs`::verify_server_cert with attacker-controlled Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this route a request to a different effective canister/subnet than the one validated or certified, violating the invariant that rate limiting and routing must not bypass ingress validation or expose privileged operations, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/tls_verify.rs`::verify_server_cert
- Entrypoint: publicly reachable verification path
- Attacker controls: Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering
- Exploit idea: route a request to a different effective canister/subnet than the one validated or certified
- Invariant to test: rate limiting and routing must not bypass ingress validation or expose privileged operations
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
