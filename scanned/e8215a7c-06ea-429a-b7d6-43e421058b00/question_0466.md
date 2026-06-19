# Q466: crypto: fs ni dkg pubkey from proto rollback edge case

## Question
Can an unprivileged attacker enter through a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares and drive `rs/crypto/node_key_validation/src/proto_conversions/fs_ni_dkg.rs`::fs_ni_dkg_pubkey_from_proto with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this trigger parser ambiguity so two encodings verify as the same key or message in different layers, violating the invariant that node/canister/TLS key validation must not accept keys outside the registered identity context, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/node_key_validation/src/proto_conversions/fs_ni_dkg.rs`::fs_ni_dkg_pubkey_from_proto
- Entrypoint: a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: trigger parser ambiguity so two encodings verify as the same key or message in different layers
- Invariant to test: node/canister/TLS key validation must not accept keys outside the registered identity context
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
