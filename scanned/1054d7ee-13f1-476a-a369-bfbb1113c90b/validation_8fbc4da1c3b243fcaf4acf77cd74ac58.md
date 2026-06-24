### Title
Single Byzantine Node Forces O(n·pairing) `combine_valid_shares` Fallback on Every VetKD Request — (`rs/crypto/src/vetkd/mod.rs`, `rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs`)

---

### Summary

`verify_encrypted_key_share_internal` only checks the Ed25519 node signature over the raw share bytes. It does not verify the cryptographic relationship of the BLS12-381 curve points inside the share. A single Byzantine node can therefore submit a share whose curve points are individually valid (passing deserialization) but whose pairing equations are wrong. This causes `EncryptedKey::combine_all` to return `InvalidShares` on every request, unconditionally triggering the expensive `combine_valid_shares` fallback, which performs O(n) pairing checks instead of O(1).

---

### Finding Description

**Step 1 — Pool admission gate only checks Ed25519.**

`crypto_verify_sig_share` in the signer calls `VetKdProtocol::verify_encrypted_key_share` before admitting a share to the validated pool: [1](#0-0) 

That call resolves to `verify_encrypted_key_share_internal`, which performs only a `BasicSigVerifierInternal::verify_basic_sig` — an Ed25519 check over the raw bytes of `encrypted_key_share`: [2](#0-1) 

There is no call to `EncryptedKeyShare::is_valid` (which would check the pairing equations). The cryptographic relationship of the three curve points (c1 ∈ G1, c2 ∈ G2, c3 ∈ G1) is never verified at admission time.

**Step 2 — Deserialization accepts any valid curve points.**

`EncryptedKeyShare::deserialize` only checks that each component is a valid curve point; it does not check the pairing equations: [3](#0-2) 

A Byzantine node can therefore craft a 192-byte blob of three valid-but-unrelated G1/G2 points, sign it with its Ed25519 key, and have it admitted to the pool.

**Step 3 — `combine_all` fails with `InvalidShares` when any share is wrong.**

`combine_all` does Lagrange interpolation over all shares (cheap), then calls `is_valid` on the combined result: [4](#0-3) 

One bad share corrupts the interpolated result, so `is_valid` fails and `InvalidShares` is returned.

**Step 4 — `InvalidShares` unconditionally triggers the expensive fallback.**

`combine_encrypted_key_shares_internal` catches `InvalidShares` and falls back to `combine_valid_shares`: [5](#0-4) 

**Step 5 — `combine_valid_shares` does O(n) pairing checks.**

`combine_valid_shares` iterates through every share and calls `node_eks.is_valid(...)` on each, which internally calls `check_validity` with two multi-pairing operations: [6](#0-5) 

`check_validity` itself performs two `Gt::multipairing` calls: [7](#0-6) 

For a 34-node subnet with threshold 23, this is up to 34 individual `is_valid` calls (stopping after 23 valid ones) versus the 1 `is_valid` call in the `combine_all` fast path — a ~23–34× increase in pairing work per request.

---

### Impact Explanation

Every `vetkd_derive_key` request processed by an honest combiner incurs O(n·pairing\_cost) work instead of O(1·pairing\_cost). For a 34-node subnet this is a ~34× slowdown in the cryptographic work per request. Under concurrent load this degrades vetkd throughput for the entire subnet. The system remains correct (honest nodes are still excluded, the final key is valid) but availability is degraded.

---

### Likelihood Explanation

The attack requires exactly one Byzantine node in the subnet committee — well below the fault threshold. The node needs only to: generate three random valid BLS12-381 curve points, concatenate them into a 192-byte blob, sign with its Ed25519 key, and broadcast. No special capability beyond subnet membership is required. The attack is persistent across all concurrent requests as long as the node remains in the committee.

---

### Recommendation

Add a cryptographic validity check inside `verify_encrypted_key_share_internal`. After the Ed25519 check passes, deserialize the share and call `EncryptedKeyShare::is_valid` with the signer's individual node public key (derivable from the NI-DKG transcript). Shares that fail this check should be rejected at pool admission, preventing them from ever reaching `combine_encrypted_key_shares_internal`. This makes the fast `combine_all` path the normal case even in the presence of Byzantine nodes.

---

### Proof of Concept

```
1. In a 13-node test committee (threshold = 9):
   - Generate 12 honest shares via EncryptedKeyShare::create
   - Generate 1 Byzantine share: pick random valid G1/G2/G1 points,
     concatenate (192 bytes), sign with the Byzantine node's Ed25519 key
   - verify_encrypted_key_share passes for the Byzantine share (Ed25519 OK)
   - Call combine_encrypted_key_shares with all 13 shares

2. Observe:
   - combine_all returns InvalidShares (combined result fails is_valid)
   - combine_valid_shares is invoked, performing 13 is_valid calls
   - Each is_valid call does 2 Gt::multipairing operations

3. Benchmark:
   - Baseline (0 bad shares): combine_all path, 1 pairing check
   - Attack (1 bad share):    combine_valid_shares path, 13 pairing checks
   - Assert latency ratio ≈ 13× (bounded only by n, not by threshold)
```

The existing benchmark at `rs/crypto/internal/crypto_lib/bls12_381/vetkd/benches/vetkd.rs` already benchmarks `combine_valid_shares` for n=13 and n=34 [8](#0-7)  but does not benchmark the `combine_all` fast path or the latency delta introduced by a single bad share — adding that comparison directly demonstrates the attack.

### Citations

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

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L157-187)
```rust
fn check_validity(
    c1: &G1Affine,
    c2: &G2Affine,
    c3: &G1Affine,
    tpk: &TransportPublicKey,
    verification_pk: &G2Affine,
    msg: &G1Affine,
) -> bool {
    let neg_g2_g = G2Prepared::neg_generator();
    let c2_prepared = G2Prepared::from(c2);

    // check e(c1,g2) == e(g1, c2)
    let c1_c2 = Gt::multipairing(&[(c1, neg_g2_g), (G1Affine::generator(), &c2_prepared)]);
    if !c1_c2.is_identity() {
        return false;
    }

    let verification_key_prepared = G2Prepared::from(verification_pk);

    // check e(c3, g2) == e(tpk, c2) * e(msg, dpki)
    let c3_c2_msg = Gt::multipairing(&[
        (c3, neg_g2_g),
        (tpk.point(), &c2_prepared),
        (msg, &verification_key_prepared),
    ]);

    if !c3_c2_msg.is_identity() {
        return false;
    }

    true
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L252-265)
```rust
    pub fn combine_all(
        nodes: &BTreeMap<NodeIndex, EncryptedKeyShare>,
        reconstruction_threshold: usize,
        master_pk: &G2Affine,
        tpk: &TransportPublicKey,
        context: &DerivationContext,
        input: &[u8],
    ) -> Result<Self, EncryptedKeyCombinationError> {
        let c = Self::combine_unchecked(nodes, reconstruction_threshold)?;
        if c.is_valid(master_pk, context, input, tpk) {
            Ok(c)
        } else {
            Err(EncryptedKeyCombinationError::InvalidShares)
        }
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L286-298)
```rust
        // Take the first reconstruction_threshold shares which pass validity check
        let mut valid_shares = BTreeMap::new();

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

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L446-464)
```rust
    pub fn deserialize(val: &[u8]) -> Result<Self, EncryptedKeyShareDeserializationError> {
        if val.len() != Self::BYTES {
            return Err(EncryptedKeyShareDeserializationError::InvalidEncryptedKeyShare);
        }
        let c2_start = G1Affine::BYTES;
        let c3_start = G1Affine::BYTES + G2Affine::BYTES;

        let c1_bytes: &[u8] = &val[..c2_start];
        let c2_bytes: &[u8] = &val[c2_start..c3_start];
        let c3_bytes: &[u8] = &val[c3_start..];

        let c1 = G1Affine::deserialize(&c1_bytes);
        let c2 = G2Affine::deserialize(&c2_bytes);
        let c3 = G1Affine::deserialize(&c3_bytes);

        match (c1, c2, c3) {
            (Ok(c1), Ok(c2), Ok(c3)) => Ok(Self { c1, c2, c3 }),
            (_, _, _) => Err(EncryptedKeyShareDeserializationError::InvalidEncryptedKeyShare),
        }
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/benches/vetkd.rs (L69-80)
```rust
        group.bench_function(
            format!("EncryptedKey::combine_valid_shares (n={nodes})"),
            |b| {
                b.iter(|| {
                    EncryptedKey::combine_valid_shares(
                        &node_info, threshold, &master_pk, &tpk, &context, &input,
                    )
                    .unwrap()
                })
            },
        );
    }
```
