### Title
Single Byzantine Node Forces O(n·pairing) Fallback on Every vetKD Combine — (`rs/crypto/src/vetkd/mod.rs`)

### Summary

`verify_encrypted_key_share_internal` only validates the Ed25519 `node_signature` over the raw share bytes. It does not verify the BLS12-381 cryptographic relationship of the `EncryptedKeyShare` content. A single Byzantine subnet member can exploit this gap to force the expensive `combine_valid_shares` fallback on every `vetkd_derive_key` request.

### Finding Description

**`verify_encrypted_key_share_internal`** performs only an Ed25519 basic-signature check: [1](#0-0) 

It never calls `EncryptedKeyShare::is_valid(...)`. A Byzantine node can therefore craft a share with:
- Valid G1/G2 curve-point encodings (so `EncryptedKeyShare::deserialize` succeeds)
- A wrong cryptographic relationship (so `is_valid` returns `false`)
- A valid Ed25519 signature over those bytes (using its own registered node key)

This share passes `verify_encrypted_key_share` and is admitted to the validated pool.

**`combine_encrypted_key_shares_internal`** then follows a two-phase strategy:

1. **Fast path** — `EncryptedKey::combine_all`: interpolates all shares, then calls `is_valid` once on the combined result. One bad share poisons the interpolation, returning `InvalidShares`. [2](#0-1) 

2. **Slow fallback** — on `InvalidShares`, the code falls back to `EncryptedKey::combine_valid_shares`, which calls `node_eks.is_valid(...)` (two multi-pairings) for every node until it accumulates `reconstruction_threshold` valid shares: [3](#0-2) [4](#0-3) 

The fallback is explicitly logged and confirmed by the existing test suite: [5](#0-4) 

### Impact Explanation

For every `vetkd_derive_key` request while the Byzantine node is active, the combiner performs O(n) BLS12-381 pairing operations instead of O(1). On a 13-node subnet (threshold 9), this is ~13 pairing checks (~1–2 ms each on modern hardware) per request instead of ~2, a roughly 6× slowdown on the combine step. Because `combine_encrypted_key_shares` is called in the consensus hot path for every vetKD response, this degrades subnet-wide vetKD throughput for the duration of the Byzantine node's participation.

### Likelihood Explanation

The precondition is one legitimate subnet member behaving maliciously — below the Byzantine fault threshold. The attack requires no privileged access beyond being a registered subnet node. The crafted share is indistinguishable from a valid share at the admission layer. The attack is persistent and repeatable across all concurrent requests.

### Recommendation

`verify_encrypted_key_share_internal` should also validate the cryptographic content of the share using `EncryptedKeyShare::is_valid(...)` (which requires the node's individual public key). This makes the admission check complete, so cryptographically invalid shares are rejected before entering the pool and never reach `combine_encrypted_key_shares`. The individual public key is already derivable from the NI-DKG transcript data available in the `lockable_threshold_sig_data_store`.

### Proof of Concept

1. Byzantine node generates three valid BLS12-381 curve points `(c1, c2, c3)` that satisfy `e(c1, g2) == e(g1, c2)` but violate `e(c3, g2) == e(tpk, c2) * e(msg, dpk_i)`.
2. Node serializes these 192 bytes, signs with its Ed25519 node key, and broadcasts the `VetKdKeyShare`.
3. Every honest node calls `verify_encrypted_key_share` → passes (Ed25519 only).
4. Share enters the validated pool.
5. `combine_encrypted_key_shares_internal` calls `combine_all` → `InvalidShares`.
6. Fallback to `combine_valid_shares` runs O(n) pairing checks.
7. Benchmark: measure `combine_encrypted_key_shares` latency with 0 vs 1 invalid share in a 13-node committee; the latency increase is proportional to n·pairing\_cost and repeats on every request.

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

**File:** rs/crypto/src/vetkd/mod.rs (L388-430)
```rust
        Err(EncryptedKeyCombinationError::InvalidShares) => {
            info!(logger, "EncryptedKey::combine_all failed with InvalidShares, \
                falling back to EncryptedKey::combine_valid_shares"
            );

            let clib_shares_for_combine_valid: BTreeMap<NodeIndex, (G2Affine, EncryptedKeyShare)> = clib_shares
                .into_iter()
                .map(|(node_id, node_index, clib_share)| {
                    let node_public_key = lazily_calculated_public_key_from_store(
                        lockable_threshold_sig_data_store,
                        threshold_sig_csp_client,
                        args.ni_dkg_id,
                        node_id,
                    )
                    .map_err(|e| {
                        VetKdKeyShareCombinationError::IndividualPublicKeyComputationError(e)
                    })?;
                    let node_public_key_g2affine = match node_public_key {
                        CspThresholdSigPublicKey::ThresBls12_381(public_key_bytes) => {
                            G2Affine::deserialize_cached(&public_key_bytes.0)
                            .map_err(|_: PairingInvalidPoint| VetKdKeyShareCombinationError::InternalError(
                                format!("individual public key of node with ID {node_id} in threshold sig data store")
                            ))
                        }
                    }?;
                    Ok((node_index, (node_public_key_g2affine, clib_share.clone())))
                })
                .collect::<Result<_, _>>()?;

            ic_crypto_internal_bls12_381_vetkd::EncryptedKey::combine_valid_shares(
                &clib_shares_for_combine_valid,
                reconstruction_threshold,
                &master_public_key,
                &transport_public_key,
                &context,
                args.input,
            )
            .map_err(|e| {
                VetKdKeyShareCombinationError::CombinationError(format!(
                    "failed to combine the valid encrypted vetKD key shares: {e:?}"
                ))
            })
        },
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L260-265)
```rust
        let c = Self::combine_unchecked(nodes, reconstruction_threshold)?;
        if c.is_valid(master_pk, context, input, tpk) {
            Ok(c)
        } else {
            Err(EncryptedKeyCombinationError::InvalidShares)
        }
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L289-298)
```rust
        for (node_index, (node_pk, node_eks)) in nodes.iter() {
            if node_eks.is_valid(master_pk, node_pk, context, input, tpk) {
                valid_shares.insert(*node_index, node_eks.clone());

                // Have we collected enough shares?
                // If so stop verifying and proceed with reconstruction
                if valid_shares.len() >= reconstruction_threshold {
                    break;
                }
            }
```

**File:** rs/crypto/tests/vetkd.rs (L396-399)
```rust
                LogEntriesAssert::assert_that(logs).has_only_one_message_containing(
                &Level::Info,
                "EncryptedKey::combine_all failed with InvalidShares, falling back to EncryptedKey::combine_valid_shares"
            );
```
