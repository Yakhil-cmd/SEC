# Q2937: boundary: retry request ordering/race

## Question
Can an unprivileged attacker enter through an API/boundary client sends crafted call, query, read_state, status, dashboard, rate-limit, or HTTP gateway requests and drive `rs/boundary_node/ic_boundary/src/http/middleware/retry.rs`::retry_request with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths, violating the invariant that boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/http/middleware/retry.rs`::retry_request
- Entrypoint: an API/boundary client sends crafted call, query, read_state, status, dashboard, rate-limit, or HTTP gateway requests
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths
- Invariant to test: boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
