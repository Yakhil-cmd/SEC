# Q3083: boundary: check certificate verification canonical encoding

## Question
Can an unprivileged attacker enter through certified-state/read_state path and drive `rs/boundary_node/ic_boundary/src/tls_verify.rs`::check_certificate_verification with attacker-controlled hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this route a request to a different effective canister/subnet than the one validated or certified, violating the invariant that rate limiting and routing must not bypass ingress validation or expose privileged operations, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/tls_verify.rs`::check_certificate_verification
- Entrypoint: certified-state/read_state path
- Attacker controls: hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing
- Exploit idea: route a request to a different effective canister/subnet than the one validated or certified
- Invariant to test: rate limiting and routing must not bypass ingress validation or expose privileged operations
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
