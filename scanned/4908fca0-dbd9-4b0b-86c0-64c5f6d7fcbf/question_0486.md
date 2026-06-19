# Q486: crypto: create dealing rollback edge case

## Question
Can an unprivileged attacker enter through a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares and drive `rs/crypto/src/sign/canister_threshold_sig/idkg/dealing.rs`::create_dealing with attacker-controlled DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs, violating the invariant that node/canister/TLS key validation must not accept keys outside the registered identity context, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/src/sign/canister_threshold_sig/idkg/dealing.rs`::create_dealing
- Entrypoint: a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares
- Attacker controls: DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records
- Exploit idea: cause complaint/opening handling to retain, remove, or reveal key material under wrong transcript IDs
- Invariant to test: node/canister/TLS key validation must not accept keys outside the registered identity context
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
