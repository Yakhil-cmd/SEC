# Q2920: boundary: Bypasser signature/domain

## Question
Can an unprivileged attacker enter through an unprivileged user sends malformed signatures, delegations, principals, or WebAuthn credentials to ingress validation and drive `rs/boundary_node/ic_boundary/src/http/middleware/cache.rs`::Bypasser with attacker-controlled Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this route a request to a different effective canister/subnet than the one validated or certified, violating the invariant that malformed public requests must fail closed without crashing in-scope platform components, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/boundary_node/ic_boundary/src/http/middleware/cache.rs`::Bypasser
- Entrypoint: an unprivileged user sends malformed signatures, delegations, principals, or WebAuthn credentials to ingress validation
- Attacker controls: Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering
- Exploit idea: route a request to a different effective canister/subnet than the one validated or certified
- Invariant to test: malformed public requests must fail closed without crashing in-scope platform components
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; mutate domain separators, registry versions, signer IDs, and message bytes independently
