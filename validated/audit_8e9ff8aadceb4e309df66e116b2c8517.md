The bug is confirmed in the code. Let me verify the exact lines and compare the Ed25519 branch for completeness.

The bug is real and confirmed. Here is the analysis:

---

### Title
`read_state` envelope embeds transaction signer's public key instead of read_state signer's public key for secp256k1 — (`rs/rosetta-api/icp/src/request_handler/construction_combine.rs`)

### Summary

In `construction_combine`, the `SignatureType::Ecdsa` branch that builds the `read_state_envelope` reads `transaction_signature.public_key.hex_bytes` on line 142 instead of `read_state_signature.public_key.hex_bytes`. The `sender_sig` on line 156 is correctly set to the read_state signature. This creates a mismatched envelope: the public key belongs to the transaction keypair while the signature was produced by the read_state keypair.

### Finding Description

In `construction_combine.rs`, two separate signatures are looked up from the client-supplied map:

- `transaction_signature` — keyed by the update message ID [1](#0-0) 
- `read_state_signature` — keyed by the read_state representation-independent hash [2](#0-1) 

When building the `read_state_envelope` for `SignatureType::Ed25519`, the code correctly uses `read_state_signature.public_key.hex_bytes` for `sender_pubkey`: [3](#0-2) 

However, in the `SignatureType::Ecdsa` branch for the same `read_state_envelope`, line 142 reads from `transaction_signature` instead: [4](#0-3) 

While `sender_sig` is correctly set to `read_state_signature.hex_bytes`: [5](#0-4) 

The result is a `read_state` envelope where `sender_pubkey` is the DER-encoded transaction public key but `sender_sig` is a signature produced by the read_state private key. The IC replica verifies that `sender_sig` is a valid signature over the request content under `sender_pubkey`; when the two keys differ, this check fails and the replica rejects the read_state request with an authentication error.

### Impact Explanation

When a Rosetta client submits two distinct secp256k1 keypairs — one for the transaction and one for the read_state — the replica accepts the update call (transaction envelope is correctly formed) but rejects every read_state poll. The Rosetta node's polling loop in `/construction/submit` never receives a certified response, so it cannot confirm finality. From the client's perspective the transaction is stuck in an unconfirmable state. The ICP transfer itself executes on-chain, but the client has no way to verify this through the Rosetta interface and may treat the transfer as failed.

### Likelihood Explanation

The Rosetta Construction API specification explicitly allows different signing keys per payload. Any client that follows this pattern with secp256k1 keys will hit this bug deterministically. The Ed25519 path is unaffected, so only secp256k1 users are impacted. The bug is reachable by any unprivileged API caller with no special access required.

### Recommendation

Change line 142 from `transaction_signature.public_key.hex_bytes` to `read_state_signature.public_key.hex_bytes`, mirroring the correct Ed25519 branch at line 121:

```rust
// line 141-143 — fix:
Secp256k1KeyPair::hex_decode_pk(
    &read_state_signature.public_key.hex_bytes,  // was: transaction_signature
)
```

### Proof of Concept

Construct a `ConstructionCombineRequest` with two distinct secp256k1 keypairs. Sign the update payload with key A and the read_state payload with key B. Call `construction_combine`. Inspect the returned `read_state_envelope.sender_pubkey` — it will DER-decode to key A's public key, not key B's. Submit the envelope pair to a replica; the update call is accepted, but the read_state poll returns an authentication error because the signature (from key B) does not verify under the embedded public key (from key A).

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L50-56)
```rust
                let transaction_signature = signatures_by_sig_data
                    .get(&make_sig_data(&update.id()))
                    .ok_or_else(|| {
                        ApiError::internal_error(
                            "Could not find signature for transaction".to_string(),
                        )
                    })?;
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L57-65)
```rust
                let read_state_signature = signatures_by_sig_data
                    .get(&make_sig_data(&MessageId::from(
                        read_state.representation_independent_hash(),
                    )))
                    .ok_or_else(|| {
                        ApiError::internal_error(
                            "Could not find signature for read-state".to_string(),
                        )
                    })?;
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L119-122)
```rust
                            Ed25519KeyPair::der_encode_pk(
                                Ed25519KeyPair::hex_decode_pk(
                                    &read_state_signature.public_key.hex_bytes,
                                )
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L139-143)
```rust
                        sender_pubkey: Some(Blob(
                            Secp256k1KeyPair::der_encode_pk(
                                Secp256k1KeyPair::hex_decode_pk(
                                    &transaction_signature.public_key.hex_bytes,
                                )
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_combine.rs (L156-157)
```rust
                        sender_sig: Some(Blob(from_hex(&read_state_signature.hex_bytes)?)),
                        sender_delegation: None,
```
