# Q2813: boundary: calculate diff canonical encoding

## Question
Can an unprivileged attacker enter through an API/boundary client sends crafted call, query, read_state, status, dashboard, rate-limit, or HTTP gateway requests and drive `rs/boundary_node/ic_boundary/src/bouncer/firewall.rs`::calculate_diff with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this route a request to a different effective canister/subnet than the one validated or certified, violating the invariant that boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/bouncer/firewall.rs`::calculate_diff
- Entrypoint: an API/boundary client sends crafted call, query, read_state, status, dashboard, rate-limit, or HTTP gateway requests
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: route a request to a different effective canister/subnet than the one validated or certified
- Invariant to test: boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
