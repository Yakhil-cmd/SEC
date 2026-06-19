# Q1486: boundary: validate webauthn sig rollback edge case

## Question
Can an unprivileged attacker enter through publicly reachable validation path and drive `rs/validator/src/webauthn.rs`::validate_webauthn_sig with attacker-controlled HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths, violating the invariant that read_state/query/certified asset responses must not be forgeable or stale beyond protocol rules, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/validator/src/webauthn.rs`::validate_webauthn_sig
- Entrypoint: publicly reachable validation path
- Attacker controls: HTTP method/path/headers/body, canister ID, subnet ID, request ID, ingress expiry, signatures, and delegations
- Exploit idea: serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths
- Invariant to test: read_state/query/certified asset responses must not be forgeable or stale beyond protocol rules
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
