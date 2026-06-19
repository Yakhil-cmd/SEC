# Q3173: boundary: set authorized principal canonical encoding

## Question
Can an unprivileged attacker enter through an API/boundary client sends crafted call, query, read_state, status, dashboard, rate-limit, or HTTP gateway requests and drive `rs/boundary_node/rate_limits/canister/state.rs`::set_authorized_principal with attacker-controlled hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch, violating the invariant that boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/rate_limits/canister/state.rs`::set_authorized_principal
- Entrypoint: an API/boundary client sends crafted call, query, read_state, status, dashboard, rate-limit, or HTTP gateway requests
- Attacker controls: hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing
- Exploit idea: bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch
- Invariant to test: boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
