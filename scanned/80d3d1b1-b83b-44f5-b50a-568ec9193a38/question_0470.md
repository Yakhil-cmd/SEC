# Q470: crypto: new and zeroize argument signature/domain

## Question
Can an unprivileged attacker enter through a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares and drive `rs/crypto/secrets_containers/src/secret_array.rs`::new_and_zeroize_argument with attacker-controlled DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this trigger parser ambiguity so two encodings verify as the same key or message in different layers, violating the invariant that node/canister/TLS key validation must not accept keys outside the registered identity context, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/secrets_containers/src/secret_array.rs`::new_and_zeroize_argument
- Entrypoint: a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares
- Attacker controls: DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records
- Exploit idea: trigger parser ambiguity so two encodings verify as the same key or message in different layers
- Invariant to test: node/canister/TLS key validation must not accept keys outside the registered identity context
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; mutate domain separators, registry versions, signer IDs, and message bytes independently
