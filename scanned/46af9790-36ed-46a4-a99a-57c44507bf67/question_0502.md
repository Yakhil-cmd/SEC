# Q502: crypto: epoch replay/idempotency

## Question
Can an unprivileged attacker enter through a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares and drive `rs/crypto/src/sign/threshold_sig/ni_dkg/utils.rs`::epoch with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to replay, repeat, or race an accepted message so the side effect is applied twice or cleanup is skipped; specifically, can this trigger parser ambiguity so two encodings verify as the same key or message in different layers, violating the invariant that node/canister/TLS key validation must not accept keys outside the registered identity context, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/src/sign/threshold_sig/ni_dkg/utils.rs`::epoch
- Entrypoint: a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: trigger parser ambiguity so two encodings verify as the same key or message in different layers
- Invariant to test: node/canister/TLS key validation must not accept keys outside the registered identity context
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; repeat the same request before and after callback/timer boundaries and assert exactly-once effects
