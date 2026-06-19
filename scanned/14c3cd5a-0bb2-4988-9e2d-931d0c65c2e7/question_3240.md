# Q3240: boundary: recompute metrics signature/domain

## Question
Can an unprivileged attacker enter through an unprivileged user sends malformed signatures, delegations, principals, or WebAuthn credentials to ingress validation and drive `rs/boundary_node/salt_sharing/canister/metrics.rs`::recompute_metrics with attacker-controlled hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this route a request to a different effective canister/subnet than the one validated or certified, violating the invariant that malformed public requests must fail closed without crashing in-scope platform components, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/salt_sharing/canister/metrics.rs`::recompute_metrics
- Entrypoint: an unprivileged user sends malformed signatures, delegations, principals, or WebAuthn credentials to ingress validation
- Attacker controls: hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing
- Exploit idea: route a request to a different effective canister/subnet than the one validated or certified
- Invariant to test: malformed public requests must fail closed without crashing in-scope platform components
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; mutate domain separators, registry versions, signer IDs, and message bytes independently
