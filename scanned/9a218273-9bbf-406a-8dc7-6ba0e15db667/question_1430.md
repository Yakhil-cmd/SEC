# Q1430: crypto: idkg transcript params proto signature/domain

## Question
Can an unprivileged attacker enter through a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares and drive `rs/types/types/src/crypto/canister_threshold_sig/idkg/proto_conversions.rs`::idkg_transcript_params_proto with attacker-controlled signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements to reuse signatures, transcripts, or request IDs across domains, algorithms, or registry versions; specifically, can this bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation, violating the invariant that node/canister/TLS key validation must not accept keys outside the registered identity context, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `rs/types/types/src/crypto/canister_threshold_sig/idkg/proto_conversions.rs`::idkg_transcript_params_proto
- Entrypoint: a protocol peer submits malformed DKG/IDKG dealings, complaints, openings, or signature shares
- Attacker controls: signature algorithms, threshold contexts, node IDs, registry versions, and malformed group elements
- Exploit idea: bypass subgroup, canonical encoding, or domain-separation checks for a threshold-signature operation
- Invariant to test: node/canister/TLS key validation must not accept keys outside the registered identity context
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants; mutate domain separators, registry versions, signer IDs, and message bytes independently
