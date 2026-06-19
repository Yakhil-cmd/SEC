# Q442: crypto: gen dealing encryption key pair replay/idempotency

## Question
Can an unprivileged attacker enter through a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares and drive `rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/ni_dkg/mod.rs`::gen_dealing_encryption_key_pair with attacker-controlled DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation, violating the invariant that node/canister/TLS key validation must not accept keys outside the registered identity context, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/ni_dkg/mod.rs`::gen_dealing_encryption_key_pair
- Entrypoint: a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares
- Attacker controls: DER/COSE/protobuf encodings, curve points, public keys, domain separators, and registry key records
- Exploit idea: bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation
- Invariant to test: node/canister/TLS key validation must not accept keys outside the registered identity context
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
