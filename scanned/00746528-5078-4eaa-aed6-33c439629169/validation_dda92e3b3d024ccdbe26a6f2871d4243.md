The code evidence confirms this is a real vulnerability. Here is the full analysis:

---

### Title
VetKD Share Verification Accepts Cryptographically Invalid Content — (`rs/crypto/src/vetkd/mod.rs`)

### Summary

`verify_encrypted_key_share_internal` only verifies the Ed25519 node signature over the raw bytes of `encrypted_key_share`, but never checks that those bytes deserialize into a valid `EncryptedKeyShare`. A single Byzantine subnet node can sign empty (or otherwise malformed) bytes with its legitimate Ed25519 key, pass share verification, enter the validated pool, and then cause `combine_encrypted_key_shares_internal` to abort at the deserialization step — permanently blocking VetKD key derivation for any targeted request.

### Finding Description

**Step 1 — Verification gap.**

`verify_encrypted_key_share_internal` does exactly two things:

1. Looks up the registry version from the threshold-sig data store.
2. Calls `BasicSigVerifierInternal::verify_basic_sig` over `key_share.encrypted_key_share` (the raw bytes). [1](#0-0) 

There is no call to `EncryptedKeyShare::deserialize` and no check that the bytes represent a valid BLS12-381 group element triple. If the Ed25519 signature is valid over empty bytes, the function returns `Ok(())`.

**Step 2 — Deserialization enforced only at combination time.**

`combine_encrypted_key_shares_internal` iterates over every share in the validated pool and calls `EncryptedKeyShare::deserialize` on each one: [2](#0-1) 

`EncryptedKeyShare::deserialize` immediately rejects any byte slice whose length differs from `2 * G1Affine::BYTES + G2Affine::BYTES`: [3](#0-2) 

The `.collect::<Result<_, _>>()?` on line 366 means a single deserialization failure aborts the entire combination, mapping to `VetKdKeyShareCombinationError::InvalidArgumentEncryptedKeyShare`. [4](#0-3) 

**Step 3 — The fallback path (`combine_valid_shares`) is never reached.**

The fallback to `EncryptedKey::combine_valid_shares` (which filters per-share validity) is only triggered when `combine_all` returns `EncryptedKeyCombinationError::InvalidShares`. The deserialization failure occurs *before* `combine_all` is ever called, so the fallback is unreachable. [5](#0-4) 

**Step 4 — The validated pool accepts the malformed share.**

`validate_signature_share` in the signer calls `crypto_verify_sig_share`, which calls `verify_encrypted_key_share`. On `Ok(())` it issues `IDkgChangeAction::MoveToValidated(share)`: [6](#0-5) [7](#0-6) 

**Step 5 — The combiner uses all validated shares.**

`combine_shares` in the chain-key payload builder passes the full validated share map to `combine_encrypted_key_shares`: [8](#0-7) 

### Impact Explanation

A single Byzantine subnet node (below the fault threshold) can permanently block VetKD key derivation for any targeted canister request. The malformed share, once in the validated pool, is included in every combination attempt. Because deserialization fails before any filtering occurs, no amount of honest shares can rescue the combination. The canister's `vetkd_derive_key` call will never complete.

### Likelihood Explanation

Any subnet node that has been compromised or is acting maliciously can execute this attack. The node needs only its own registered Ed25519 signing key (which it legitimately holds) and the ability to craft a `VetKdKeyShare` protobuf with an empty `encrypted_key_share` field. The protobuf deserialization at the P2P layer accepts arbitrary byte slices for this field: [9](#0-8) 

No special privileges, no threshold corruption, and no external dependencies are required.

### Recommendation

Add `EncryptedKeyShare::deserialize` validation inside `verify_encrypted_key_share_internal`, immediately after the signature check. If deserialization fails, return a `VetKdKeyShareVerificationError` (e.g., a new `InvalidEncryptedKeyShareContent` variant, or reuse `VerificationError` with an appropriate `CryptoError::InvalidArgument`). This ensures that any share that cannot be deserialized is rejected at admission time and never enters the validated pool.

### Proof of Concept

```rust
// Pseudocode unit test
let byzantine_node_id = ...; // valid subnet node with registered Ed25519 key
let empty_content = VetKdEncryptedKeyShareContent(vec![]);
// Sign the empty bytes with the node's legitimate Ed25519 key
let sig = ed25519_sign(&empty_content.0, &byzantine_node_sk);
let malformed_share = VetKdEncryptedKeyShare {
    encrypted_key_share: empty_content,
    node_signature: sig,
};
// verify_encrypted_key_share returns Ok(()) — signature is valid
assert_eq!(
    crypto.verify_encrypted_key_share(byzantine_node_id, &malformed_share, &args),
    Ok(())  // BUG: should be Err(...)
);
// combine_encrypted_key_shares fails when this share is included
let mut shares = honest_shares; // threshold-many honest shares
shares.insert(byzantine_node_id, malformed_share);
assert!(matches!(
    crypto.combine_encrypted_key_shares(&shares, &args),
    Err(VetKdKeyShareCombinationError::InvalidArgumentEncryptedKeyShare)
));
```

### Citations

**File:** rs/crypto/src/vetkd/mod.rs (L292-301)
```rust
    let signature = BasicSigOf::new(BasicSig(key_share.node_signature.clone()));
    BasicSigVerifierInternal::verify_basic_sig(
        csp_signer,
        registry,
        &signature,
        &key_share.encrypted_key_share,
        signer,
        registry_version_from_store,
    )
    .map_err(VetKdKeyShareVerificationError::VerificationError)
```

**File:** rs/crypto/src/vetkd/mod.rs (L347-366)
```rust
    let clib_shares: Vec<(NodeId, NodeIndex, EncryptedKeyShare)> = shares
        .iter()
        .map(|(&node_id, share)| {
            let node_index = transcript_data_from_store.index(node_id).ok_or(
                VetKdKeyShareCombinationError::InternalError(format!(
                    "missing index for node with ID {node_id} in threshold \
                        sig data store for NI-DKG ID {}",
                    args.ni_dkg_id
                )),
            )?;
            let clib_share = EncryptedKeyShare::deserialize(&share.encrypted_key_share.0).map_err(
                |e| match e {
                    EncryptedKeyShareDeserializationError::InvalidEncryptedKeyShare => {
                        VetKdKeyShareCombinationError::InvalidArgumentEncryptedKeyShare
                    }
                },
            )?;
            Ok((node_id, *node_index, clib_share))
        })
        .collect::<Result<_, _>>()?;
```

**File:** rs/crypto/src/vetkd/mod.rs (L373-392)
```rust
    match ic_crypto_internal_bls12_381_vetkd::EncryptedKey::combine_all(
        &clib_shares_for_combine_all,
        reconstruction_threshold,
        &master_public_key,
        &transport_public_key,
        &context,
        args.input,
    ) {
        Ok(encrypted_key) => Ok(encrypted_key),
        Err(EncryptedKeyCombinationError::InsufficientShares) => {
            Err(VetKdKeyShareCombinationError::UnsatisfiedReconstructionThreshold {
                threshold: reconstruction_threshold,
                share_count: clib_shares_for_combine_all.len()
            })
        }
        Err(EncryptedKeyCombinationError::InvalidShares) => {
            info!(logger, "EncryptedKey::combine_all failed with InvalidShares, \
                falling back to EncryptedKey::combine_valid_shares"
            );

```

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L446-449)
```rust
    pub fn deserialize(val: &[u8]) -> Result<Self, EncryptedKeyShareDeserializationError> {
        if val.len() != Self::BYTES {
            return Err(EncryptedKeyShareDeserializationError::InvalidEncryptedKeyShare);
        }
```

**File:** rs/types/types/src/crypto/vetkd.rs (L141-153)
```rust
pub enum VetKdKeyShareCombinationError {
    ThresholdSigDataNotFound(ThresholdSigDataNotFoundError),
    InvalidArgumentMasterPublicKey,
    InvalidArgumentEncryptionPublicKey,
    InvalidArgumentEncryptedKeyShare,
    IndividualPublicKeyComputationError(CryptoError),
    CombinationError(String),
    InternalError(String),
    UnsatisfiedReconstructionThreshold {
        threshold: usize,
        share_count: usize,
    },
}
```

**File:** rs/consensus/idkg/src/signer.rs (L308-323)
```rust
            Ok(share) => {
                self.metrics.sign_metrics_inc("sig_shares_received");

                // Although we already checked the cache for duplicate shares above, it could happen that a
                // different thread validated a share for the same request_id in the meantime, after we
                // released the read lock. Therefore, we acquire the write lock here to check again with
                // exclusive access.
                let mut valid_sig_share_signers = self.validated_sig_share_signers.write().unwrap();
                let signers = valid_sig_share_signers.entry(request_id).or_default();
                if !signers.insert(signer) {
                    self.metrics
                        .sign_errors_inc("duplicate_sig_share_cache_miss");
                    Some(IDkgChangeAction::RemoveUnvalidated(id))
                } else {
                    Some(IDkgChangeAction::MoveToValidated(share))
                }
```

**File:** rs/consensus/idkg/src/signer.rs (L513-523)
```rust
            (ThresholdSigInputs::VetKd(inputs), SigShare::VetKd(share)) => {
                VetKdProtocol::verify_encrypted_key_share(
                    &*self.crypto,
                    share.signer_id,
                    &share.share,
                    inputs,
                )
                .map_or_else(
                    |err| Err(VerifySigShareError::VetKd(err)),
                    |_| Ok(IDkgMessage::VetKdKeyShare(share)),
                )
```

**File:** rs/consensus/chain_key/src/lib.rs (L247-265)
```rust
            ThresholdSigInputs::VetKd(args) => {
                let shares = shares
                    .vetkd_shares
                    .get(callback_id)
                    .ok_or(CombineSharesError::NoSharesFound)?;
                self.crypto
                    .combine_encrypted_key_shares(shares, args)
                    .map_or_else(
                        |err| Err(CombineSharesError::VetKd(err)),
                        |key| {
                            Ok(ChainKeyAgreement::Success(
                                VetKdDeriveKeyResult {
                                    encrypted_key: key.encrypted_key,
                                }
                                .encode(),
                            ))
                        },
                    )
            }
```

**File:** rs/types/types/src/consensus/idkg.rs (L1551-1562)
```rust
impl TryFrom<pb::VetKdKeyShare> for VetKdKeyShare {
    type Error = ProxyDecodeError;
    fn try_from(value: pb::VetKdKeyShare) -> Result<Self, Self::Error> {
        Ok(Self {
            signer_id: node_id_try_from_option(value.signer_id)?,
            request_id: try_from_option_field(value.request_id, "VetKdKeyShare::request_id")?,
            share: VetKdEncryptedKeyShare {
                encrypted_key_share: VetKdEncryptedKeyShareContent(value.encrypted_key_share),
                node_signature: value.node_signature,
            },
        })
    }
```
