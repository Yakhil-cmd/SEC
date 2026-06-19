# Q1592: boundary: raw query param returns none for missing key replay/idempotency

## Question
Can an unprivileged attacker enter through public query endpoint and drive `packages/ic-http-types/src/lib.rs`::raw_query_param_returns_none_for_missing_key with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this route a request to a different effective canister/subnet than the one validated or certified, violating the invariant that malformed public requests must fail closed without crashing in-scope platform components, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `packages/ic-http-types/src/lib.rs`::raw_query_param_returns_none_for_missing_key
- Entrypoint: public query endpoint
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: route a request to a different effective canister/subnet than the one validated or certified
- Invariant to test: malformed public requests must fail closed without crashing in-scope platform components
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
