# Q1696: crypto: find leaf single shard target equals label rollback edge case

## Question
Can an unprivileged attacker enter through an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures and drive `packages/ic-signature-verification/src/canister_sig.rs`::find_leaf_single_shard_target_equals_label with attacker-controlled transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes to force an error path after partial mutation and verify the state transition rolls back completely; specifically, can this trigger parser ambiguity so two encodings verify as the same key or message in different layers, violating the invariant that malformed encodings and invalid curve/group elements must be rejected before key use, and produce HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass?

## Target
- File/function: `packages/ic-signature-verification/src/canister_sig.rs`::find_leaf_single_shard_target_equals_label
- Entrypoint: an attacker crafts encoded public keys, transcripts, derivation paths, domain separators, or signatures
- Attacker controls: transcripts, dealings, complaints, openings, signature shares, derivation paths, and message bytes
- Exploit idea: trigger parser ambiguity so two encodings verify as the same key or message in different layers
- Invariant to test: malformed encodings and invalid curve/group elements must be rejected before key use
- Expected HackenProof impact: HackenProof Critical/High: subnet key-share disclosure, unauthorized threshold signature, or authentication bypass
- Fast validation: fuzz encodings/transcripts/signature shares and assert verification rejects cross-domain or malformed variants
