# Q673: boundary: route canonical encoding

## Question
Can an unprivileged attacker enter through an API/boundary client sends crafted call, query, read_state, status, dashboard, rate-limit, or HTTP gateway requests and drive `rs/http_endpoints/public/src/pprof.rs`::route with attacker-controlled Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths, violating the invariant that boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `rs/http_endpoints/public/src/pprof.rs`::route
- Entrypoint: an API/boundary client sends crafted call, query, read_state, status, dashboard, rate-limit, or HTTP gateway requests
- Attacker controls: Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering
- Exploit idea: serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths
- Invariant to test: boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
