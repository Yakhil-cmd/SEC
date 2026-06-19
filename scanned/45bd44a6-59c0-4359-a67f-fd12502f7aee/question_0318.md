# Q318: crypto: verify basic sig by public key bounds/overflow

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `rs/crypto/interfaces/sig_verification/src/lib.rs`::verify_basic_sig_by_public_key with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this trigger parser ambiguity so two encodings verify as the same key or message in different layers, violating the invariant that node/canister/TLS key validation must not accept keys outside the registered identity context, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/interfaces/sig_verification/src/lib.rs`::verify_basic_sig_by_public_key
- Entrypoint: publicly reachable verification path
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: trigger parser ambiguity so two encodings verify as the same key or message in different layers
- Invariant to test: node/canister/TLS key validation must not accept keys outside the registered identity context
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
