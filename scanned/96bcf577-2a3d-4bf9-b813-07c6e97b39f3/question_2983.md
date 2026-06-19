# Q2983: boundary: With Metrics Snapshot canonical encoding

## Question
Can an unprivileged attacker enter through a caller repeats or races boundary forwarding, validation, retry, cache, and certified-response paths and drive `rs/boundary_node/ic_boundary/src/metrics.rs`::WithMetricsSnapshot with attacker-controlled hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths, violating the invariant that rate limiting and routing must not bypass ingress validation or expose privileged operations, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/metrics.rs`::WithMetricsSnapshot
- Entrypoint: a caller repeats or races boundary forwarding, validation, retry, cache, and certified-response paths
- Attacker controls: hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing
- Exploit idea: serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths
- Invariant to test: rate limiting and routing must not bypass ingress validation or expose privileged operations
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
