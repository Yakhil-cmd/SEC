# Q3086: boundary: Server Cert Verifier rollback edge case

## Question
Can an unprivileged attacker enter through certified-state/read_state path and drive `rs/boundary_node/ic_boundary/src/tls_verify.rs`::ServerCertVerifier with attacker-controlled hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this trigger non-volumetric platform-level crash via a malformed but protocol-valid API request, violating the invariant that read_state/query/certified asset responses must not be forgeable or stale beyond protocol rules, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/tls_verify.rs`::ServerCertVerifier
- Entrypoint: certified-state/read_state path
- Attacker controls: hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing
- Exploit idea: trigger non-volumetric platform-level crash via a malformed but protocol-valid API request
- Invariant to test: read_state/query/certified asset responses must not be forgeable or stale beyond protocol rules
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
