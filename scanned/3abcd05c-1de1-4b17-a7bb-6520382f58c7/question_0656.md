# Q656: boundary: abort on panic rollback edge case

## Question
Can an unprivileged attacker enter through an unprivileged user sends malformed signatures, delegations, principals, or WebAuthn credentials to ingress validation and drive `rs/http_endpoints/async_utils/src/lib.rs`::abort_on_panic with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this trigger non-volumetric platform-level crash via a malformed but protocol-valid API request, violating the invariant that malformed public requests must fail closed without crashing in-scope platform components, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/http_endpoints/async_utils/src/lib.rs`::abort_on_panic
- Entrypoint: an unprivileged user sends malformed signatures, delegations, principals, or WebAuthn credentials to ingress validation
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: trigger non-volumetric platform-level crash via a malformed but protocol-valid API request
- Invariant to test: malformed public requests must fail closed without crashing in-scope platform components
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
