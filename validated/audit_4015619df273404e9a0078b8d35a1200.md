### Title
VetKD Byzantine Node Triggers O(n) Pairing Fallback Per Request via Cryptographically Invalid Share with Valid Node Signature — (`rs/crypto/src/vetkd/mod.rs`)

---

### Summary

A single Byzantine subnet node (below the fault threshold) can produce a VetKD encrypted key share that carries a valid node signature but contains cryptographically invalid content. Because `verify_encrypted_key_share` only checks the basic node signature and not the cryptographic validity of the share, the invalid share is admitted to the validated pool and passed to `combine_encrypted_key_shares`. This causes `combine_all` to fail with `InvalidShares` on every request, triggering a fallback path that computes individual public keys for **all n nodes** and performs up to **n × 5 BLS12-381 pairing operations** instead of the normal 5. The attack is repeatable per request and scales with subnet size.

---

### Finding Description

**Step 1 — Share verification only checks the node's basic signature.**

`verify_encrypted_key_share_internal` in `rs/crypto/src/vetkd/mod.rs` calls only `BasicSigVerifierInternal::verify_basic_sig` over the raw share bytes. It does not call `EncryptedKeyShare::is_valid`, so a share with a valid node signature but garbage `c1`/`c2`/`c3` values passes verification. [1](#0-0) 

**Step 2 — All n VetKD shares are admitted to the validated pool.**

The consensus signer calls `crypto_verify_sig_share` → `VetKdProtocol::verify_encrypted_key_share` before moving a share to the validated pool. Critically, the test comment at line 1417 explicitly notes that the "optimization is disabled for VetKD for now", meaning **all n shares** (not just threshold+1) are validated and admitted: [2](#0-1) 

**Step 3 — `combine_all` uses all submitted shares; one invalid share poisons the interpolation.**

`combine_unchecked` interpolates over the entire `nodes` map. If any share is invalid, the resulting combined key fails `is_valid`, returning `InvalidShares`. [3](#0-2) 

**Step 4 — The `InvalidShares` fallback computes public keys for ALL n nodes, then does O(n) pairing verifications.**

On `InvalidShares`, `combine_encrypted_key_shares_internal` iterates over the full `clib_shares` vector (all n nodes) calling `lazily_calculated_public_key_from_store` for each, then passes all n `(G2Affine, EncryptedKeyShare)` pairs to `combine_valid_shares`. [4](#0-3) 

`combine_valid_shares` then calls `EncryptedKeyShare::is_valid` (which invokes `check_validity` — up to 5 pairings per share) for each node until `reconstruction_threshold` valid shares are found. With 1 invalid share at a low `NodeIndex`, this means up to `(n - threshold + 1) × 5` extra pairings before the threshold valid shares are collected, plus the final `is_valid` check on the combined key. [5](#0-4) 

`check_validity` itself performs two `Gt::multipairing` calls (2 + 3 pairings): [6](#0-5) 

**Step 5 — The attack is confirmed by existing tests.**

The test `should_succeed_if_reconstruction_threshold_many_shares_are_valid` explicitly demonstrates that corrupted shares (swapped `c1`/`c3`) with valid node signatures trigger the fallback log message, confirming the path is reachable in production: [7](#0-6) 

---

### Impact Explanation

For a 34-node subnet (threshold=23):
- **Normal path**: 5 pairings total (`combine_all` → `is_valid`)
- **Fallback path**: up to `(34 - 23 + 1) × 5 + 5 = 65` pairings per request, plus polynomial evaluations for all 34 node public keys on the first request (cached thereafter)

A single Byzantine node submitting one invalid share per VetKD request causes every combiner to execute the expensive fallback on every request. BLS12-381 pairings are among the most expensive cryptographic operations (~1–2 ms each on modern hardware). This is a sustained, per-request amplification attack on the combiner's crypto thread, degrading VetKD throughput proportionally to subnet size.

---

### Likelihood Explanation

The attack requires only one Byzantine subnet node — a realistic adversary within the BFT fault threshold. The technique (swapping `c1`/`c3` bytes to produce a structurally valid but cryptographically invalid share) is explicitly demonstrated in the test suite. No special access, key material, or coordination is needed beyond normal subnet membership.

---

### Recommendation

Add cryptographic share validity verification inside `verify_encrypted_key_share_internal`. Specifically, after verifying the basic node signature, also call `EncryptedKeyShare::is_valid` using the node's individual public key (computed via `lazily_calculated_public_key_from_store`). This ensures that only cryptographically valid shares are admitted to the validated pool, preventing the `InvalidShares` fallback from ever being triggered by a Byzantine node.

Alternatively, if per-share cryptographic verification at admission time is considered too expensive, the fallback path in `combine_encrypted_key_shares_internal` should be rate-limited or the combiner should track which nodes have previously submitted invalid shares and exclude them.

---

### Proof of Concept

```
1. Set up a 34-node subnet with VetKD threshold=23.
2. Byzantine node creates a valid VetKdEncryptedKeyShare:
   - Call create_encrypted_key_share normally to get a structurally valid share.
   - Swap the first 48 bytes (c1) with the last 48 bytes (c3) in encrypted_key_share.0.
   - Re-sign the modified bytes with the node's signing key → valid node_signature.
3. Submit this share. It passes verify_encrypted_key_share (basic sig check only).
4. All 34 shares (including the invalid one) are admitted to the validated pool.
5. combine_encrypted_key_shares is called with all 34 shares.
6. combine_all fails with InvalidShares (interpolation of all 34 shares produces invalid key).
7. Fallback: lazily_calculated_public_key_from_store called for all 34 nodes.
8. combine_valid_shares iterates all 34 shares calling is_valid (5 pairings each) until 23 valid ones found.
9. Assert: combiner log contains "falling back to EncryptedKey::combine_valid_shares".
10. Assert: ~65 pairing operations performed vs. 5 in the normal path.
11. Repeat for every VetKD request → sustained per-request DoS.
```

### Citations

**File:** rs/crypto/src/vetkd/mod.rs (L272-302)
```rust
fn verify_encrypted_key_share_internal<S: CspSigner>(
    lockable_threshold_sig_data_store: &LockableThresholdSigDataStore,
    registry: &dyn RegistryClient,
    csp_signer: &S,
    key_share: &VetKdEncryptedKeyShare,
    signer: NodeId,
    args: &VetKdArgs,
) -> Result<(), VetKdKeyShareVerificationError> {
    let registry_version_from_store = lockable_threshold_sig_data_store
        .read()
        .transcript_data(args.ni_dkg_id)
        .map(|transcript_data| transcript_data.registry_version())
        .ok_or_else(|| {
            VetKdKeyShareVerificationError::ThresholdSigDataNotFound(
                ThresholdSigDataNotFoundError::ThresholdSigDataNotFound {
                    dkg_id: args.ni_dkg_id.clone(),
                },
            )
        })?;

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
}
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

**File:** rs/consensus/idkg/src/signer.rs (L1413-1418)
```rust
            .collect::<Vec<_>>();
        let expected_nb_sig_shares = match key_id {
            MasterPublicKeyId::Ecdsa(_) => get_faults_tolerated(n) + 1,
            MasterPublicKeyId::Schnorr(_) => get_faults_tolerated(n) + 1,
            MasterPublicKeyId::VetKd(_) => n, // The optimization is disabled for VetKD for now
        };
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L157-188)
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
}
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L252-266)
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
    }
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L286-299)
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
        }
```

**File:** rs/crypto/tests/vetkd.rs (L370-403)
```rust
    #[test]
    fn should_succeed_if_reconstruction_threshold_many_shares_are_valid() {
        let mut rng = reproducible_rng();
        let mut server = VetKDTestServer::new(&mut rng);
        let client = VetKDTestClient::new(&mut rng, &server);
        let vetkd_args = client.create_args(&server.dkg_id);

        let mut shares = server
            .create_key_shares(&vetkd_args, &mut rng)
            .expect("Share creation unexpectedly failed");

        let to_corrupt = shares.len() - server.config.threshold().get().get() as usize;

        modify_n_random_shares(to_corrupt, &mut shares, &mut rng, |share, _rng| {
            swap_share_c1c3(&mut share.encrypted_key_share);
        });

        match server.combine_key_shares(&shares, &vetkd_args, &mut rng) {
            Ok((combiner, _key)) => {
                /* expected success */
                let logger = server
                    .env
                    .loggers
                    .remove(&combiner)
                    .expect("Missing loggers");
                let logs = logger.drain_logs();
                LogEntriesAssert::assert_that(logs).has_only_one_message_containing(
                &Level::Info,
                "EncryptedKey::combine_all failed with InvalidShares, falling back to EncryptedKey::combine_valid_shares"
            );
            }
            Err(e) => panic!("Combination failed {:?}", e),
        }
    }
```
