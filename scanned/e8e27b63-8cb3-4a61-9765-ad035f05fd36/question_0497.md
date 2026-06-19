# Q497: crypto: From ordering/race

## Question
Can an unprivileged attacker enter through a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs and drive `rs/crypto/src/sign/threshold_sig/ni_dkg/dealing/error_conversions.rs`::From with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to reorder callbacks, gossip artifacts, or timer events to violate exactly-once semantics; specifically, can this make verification accept a share/transcript under the wrong domain, registry version, or signer set, violating the invariant that threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/crypto/src/sign/threshold_sig/ni_dkg/dealing/error_conversions.rs`::From
- Entrypoint: a canister requests threshold ECDSA/Schnorr/vetKD signatures through public management APIs
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: make verification accept a share/transcript under the wrong domain, registry version, or signer set
- Invariant to test: threshold protocols must not leak key shares or sign unauthorized messages below threshold compromise
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
