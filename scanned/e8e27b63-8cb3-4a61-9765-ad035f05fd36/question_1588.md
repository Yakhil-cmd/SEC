# Q1588: boundary: path returns full url when no query string bounds/overflow

## Question
Can an unprivileged attacker enter through public query endpoint and drive `packages/ic-http-types/src/lib.rs`::path_returns_full_url_when_no_query_string with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this route a request to a different effective canister/subnet than the one validated or certified, violating the invariant that malformed public requests must fail closed without crashing in-scope platform components, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `packages/ic-http-types/src/lib.rs`::path_returns_full_url_when_no_query_string
- Entrypoint: public query endpoint
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: route a request to a different effective canister/subnet than the one validated or certified
- Invariant to test: malformed public requests must fail closed without crashing in-scope platform components
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
