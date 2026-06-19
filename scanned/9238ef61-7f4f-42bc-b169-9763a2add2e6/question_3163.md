# Q3163: boundary: recompute metrics canonical encoding

## Question
Can an unprivileged attacker enter through a caller repeats or races boundary forwarding, validation, retry, cache, and certified-response paths and drive `rs/boundary_node/rate_limits/canister/metrics.rs`::recompute_metrics with attacker-controlled Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch, violating the invariant that rate limiting and routing must not bypass ingress validation or expose privileged operations, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/rate_limits/canister/metrics.rs`::recompute_metrics
- Entrypoint: a caller repeats or races boundary forwarding, validation, retry, cache, and certified-response paths
- Attacker controls: Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering
- Exploit idea: bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch
- Invariant to test: rate limiting and routing must not bypass ingress validation or expose privileged operations
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
