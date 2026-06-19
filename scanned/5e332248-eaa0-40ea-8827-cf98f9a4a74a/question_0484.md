# Q484: crypto: create dealing resource accounting

## Question
Can an unprivileged attacker enter through an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures and drive `rs/crypto/src/sign/canister_threshold_sig/idkg.rs`::create_dealing with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to make fees, cycles, memory, instructions, or refunds diverge across success, reject, trap, and retry paths; specifically, can this make verification accept a share/transcript under the wrong domain, registry version, or signer set, violating the invariant that malformed encodings and invalid curve/group elements must be rejected before key use, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/src/sign/canister_threshold_sig/idkg.rs`::create_dealing
- Entrypoint: an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: make verification accept a share/transcript under the wrong domain, registry version, or signer set
- Invariant to test: malformed encodings and invalid curve/group elements must be rejected before key use
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
