# Q3079: boundary: Subnet certification/witness

## Question
Can an unprivileged attacker enter through a caller repeats or races boundary forwarding, validation, retry, cache, and certified-response paths and drive `rs/boundary_node/ic_boundary/src/snapshot.rs`::Subnet with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to request or construct certified data with ambiguous paths, labels, or stale state; specifically, can this bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch, violating the invariant that rate limiting and routing must not bypass ingress validation or expose privileged operations, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/snapshot.rs`::Subnet
- Entrypoint: a caller repeats or races boundary forwarding, validation, retry, cache, and certified-response paths
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch
- Invariant to test: rate limiting and routing must not bypass ingress validation or expose privileged operations
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
