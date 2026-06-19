# Q3131: boundary: Adds Config authorization boundary

## Question
Can an unprivileged attacker enter through a caller repeats or races boundary forwarding, validation, retry, cache, and certified-response paths and drive `rs/boundary_node/rate_limits/canister/add_config.rs`::AddsConfig with attacker-controlled Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering to bypass authorization by desynchronizing caller identity, effective canister/account, and validated state; specifically, can this serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths, violating the invariant that rate limiting and routing must not bypass ingress validation or expose privileged operations, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/rate_limits/canister/add_config.rs`::AddsConfig
- Entrypoint: a caller repeats or races boundary forwarding, validation, retry, cache, and certified-response paths
- Attacker controls: Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering
- Exploit idea: serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths
- Invariant to test: rate limiting and routing must not bypass ingress validation or expose privileged operations
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs
