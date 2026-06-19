# Q2852: boundary: Registry replay/idempotency

## Question
Can an unprivileged attacker enter through an unprivileged user sends malformed signatures, delegations, principals, or WebAuthn credentials to ingress validation and drive `rs/boundary_node/ic_boundary/src/cli.rs`::Registry with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch, violating the invariant that malformed public requests must fail closed without crashing in-scope platform components, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/cli.rs`::Registry
- Entrypoint: an unprivileged user sends malformed signatures, delegations, principals, or WebAuthn credentials to ingress validation
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch
- Invariant to test: malformed public requests must fail closed without crashing in-scope platform components
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
