# Q1581: boundary: bad request authorization boundary

## Question
Can an unprivileged attacker enter through an API/boundary client sends crafted call, query, read_state, status, dashboard, rate-limit, or HTTP gateway requests and drive `packages/ic-http-types/src/lib.rs`::bad_request with attacker-controlled hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths, violating the invariant that boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `packages/ic-http-types/src/lib.rs`::bad_request
- Entrypoint: an API/boundary client sends crafted call, query, read_state, status, dashboard, rate-limit, or HTTP gateway requests
- Attacker controls: hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing
- Exploit idea: serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths
- Invariant to test: boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
