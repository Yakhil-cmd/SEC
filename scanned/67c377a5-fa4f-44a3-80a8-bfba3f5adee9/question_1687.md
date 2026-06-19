# Q1687: crypto: verify certificate ordering/race

## Question
Can an unprivileged attacker enter through publicly reachable verification path and drive `packages/ic-signature-verification/src/canister_sig.rs`::verify_certificate with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this make verification accept a share/transcript under the wrong domain, registry version, or signer set, violating the invariant that cryptographic verification must bind message, domain, signer set, registry version, and transcript ID, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `packages/ic-signature-verification/src/canister_sig.rs`::verify_certificate
- Entrypoint: publicly reachable verification path
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: make verification accept a share/transcript under the wrong domain, registry version, or signer set
- Invariant to test: cryptographic verification must bind message, domain, signer set, registry version, and transcript ID
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
