# Q10: boundary: path signature/domain

## Question
Can an unprivileged attacker enter through a web client supplies hostnames, effective canister IDs, request IDs, certified asset paths, and ingress messages and drive `packages/ic-http-types/src/lib.rs`::path with attacker-controlled hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch, violating the invariant that read_state/query/certified asset responses must not be forgeable or stale beyond protocol rules, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `packages/ic-http-types/src/lib.rs`::path
- Entrypoint: a web client supplies hostnames, effective canister IDs, request IDs, certified asset paths, and ingress messages
- Attacker controls: hostnames, raw/certified asset routing, rate-limit keys, read_state paths, cache state, and retry timing
- Exploit idea: bypass ingress signature/delegation/WebAuthn validation through encoding or request-ID mismatch
- Invariant to test: read_state/query/certified asset responses must not be forgeable or stale beyond protocol rules
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; mutate domain separators, registry versions, signer IDs, and message bytes independently
