# Q1591: boundary: raw query param returns none for empty query string authorization boundary

## Question
Can an unprivileged attacker enter through public query endpoint and drive `packages/ic-http-types/src/lib.rs`::raw_query_param_returns_none_for_empty_query_string with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this trigger non-volumetric platform-level crash via a malformed but protocol-valid API request, violating the invariant that rate limiting and routing must not bypass ingress validation or expose privileged operations, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `packages/ic-http-types/src/lib.rs`::raw_query_param_returns_none_for_empty_query_string
- Entrypoint: public query endpoint
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: trigger non-volumetric platform-level crash via a malformed but protocol-valid API request
- Invariant to test: rate limiting and routing must not bypass ingress validation or expose privileged operations
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
