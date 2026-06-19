# Q1518: crypto: sign message bounds/overflow

## Question
Can an unprivileged attacker enter through public signature or threshold-signing request path and drive `packages/ic-ed25519/src/lib.rs`::sign_message with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation, violating the invariant that node/canister/TLS key validation must not accept keys outside the registered identity context, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `packages/ic-ed25519/src/lib.rs`::sign_message
- Entrypoint: public signature or threshold-signing request path
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation
- Invariant to test: node/canister/TLS key validation must not accept keys outside the registered identity context
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
