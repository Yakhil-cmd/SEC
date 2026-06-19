# Q718: crypto: create dealing bounds/overflow

## Question
Can an unprivileged attacker enter through a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares and drive `rs/interfaces/src/crypto/sign/canister_threshold_sig.rs`::create_dealing with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to hit boundary values for lengths, amounts, timestamps, heights, and indexes to cause aliasing or overflow; specifically, can this make verification accept a share/transcript under the wrong domain, registry version, or signer set, violating the invariant that node/canister/TLS key validation must not accept keys outside the registered identity context, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/interfaces/src/crypto/sign/canister_threshold_sig.rs`::create_dealing
- Entrypoint: a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: make verification accept a share/transcript under the wrong domain, registry version, or signer set
- Invariant to test: node/canister/TLS key validation must not accept keys outside the registered identity context
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; include min/max amounts, zero values, max heights, oversized payloads, and duplicate IDs
