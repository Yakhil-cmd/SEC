# Q3202: boundary: Output Config replay/idempotency

## Question
Can an unprivileged attacker enter through a web client supplies hostnames, effective canister IDs, request IDs, certified asset paths, and ingress messages and drive `rs/boundary_node/rate_limits/canister/types.rs`::OutputConfig with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this trigger non-volumetric platform-level crash via a malformed but protocol-valid API request, violating the invariant that read_state/query/certified asset responses must not be forgeable or stale beyond protocol rules, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/rate_limits/canister/types.rs`::OutputConfig
- Entrypoint: a web client supplies hostnames, effective canister IDs, request IDs, certified asset paths, and ingress messages
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: trigger non-volumetric platform-level crash via a malformed but protocol-valid API request
- Invariant to test: read_state/query/certified asset responses must not be forgeable or stale beyond protocol rules
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
