# Q513: crypto: verify basic sig by public key canonical encoding

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `rs/crypto/standalone-sig-verifier/src/lib.rs`::verify_basic_sig_by_public_key with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to use alternative encodings or unknown fields so validation and execution interpret different values; specifically, can this make verification accept a share/transcript under the wrong domain, registry version, or signer set, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/standalone-sig-verifier/src/lib.rs`::verify_basic_sig_by_public_key
- Entrypoint: publicly reachable verification path
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: make verification accept a share/transcript under the wrong domain, registry version, or signer set
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; add differential encode/decode cases for unknown fields, alternate lengths, and canonical hashes
