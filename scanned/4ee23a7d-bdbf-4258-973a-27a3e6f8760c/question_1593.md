# Q1593: boundary: raw query param returns empty value for key without value canonical encoding

## Question
Can an unprivileged attacker enter through public query endpoint and drive `packages/ic-http-types/src/lib.rs`::raw_query_param_returns_empty_value_for_key_without_value with attacker-controlled Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths, violating the invariant that boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response, and produce HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash?

## Target
- File/function: `packages/ic-http-types/src/lib.rs`::raw_query_param_returns_empty_value_for_key_without_value
- Entrypoint: public query endpoint
- Attacker controls: Candid/CBOR/protobuf payloads, WebAuthn fields, effective-canister ID, and boundary node request ordering
- Exploit idea: serve stale or forged certified data by confusing cache keys, hostnames, or read_state witness paths
- Invariant to test: boundary/API validation must bind request ID, caller, effective canister ID, subnet, and certified response
- Expected HackenProof impact: HackenProof High/Medium: boundary validation bypass, forged certified response, limited key/session exposure, or platform-level crash
- Fast validation: send crafted HTTP/API requests in a local boundary/replica setup and compare validation, routing, and certification outputs; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
