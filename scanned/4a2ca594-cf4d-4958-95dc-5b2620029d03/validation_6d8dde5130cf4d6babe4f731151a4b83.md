### Title
Byzantine Node Forces O(n·pairing) vetKD Fallback via Cryptographically Invalid but Signature-Valid Share — (`rs/crypto/src/vetkd/mod.rs`, `rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs`)

---

### Summary

`verify_encrypted_key_share_internal` only checks the node's basic signature over the raw share bytes. It does not check cryptographic validity (no pairing equations, no `is_valid` call). A single Byzantine subnet node can therefore submit a share that is structurally valid (passes G1/G2 deserialization), carries a valid node signature (passes pool admission), but fails the pairing check inside `combine_all`. This deterministically triggers the O(n·pairing) `combine_valid_shares` fallback on every vetKD request for the lifetime of that node's participation.

---

### Finding Description

**Step 1 — Share admission guard is signature-only.**

`verify_encrypted_key_share_internal` in `rs/crypto/src/vetkd/mod.rs` performs exactly one check:

```rust
BasicSigVerifierInternal::verify_basic_sig(
    csp_signer, registry, &signature,
    &key_share.encrypted_key_share,   // raw bytes
    signer, registry_version_from_store,
)
``` [1](#0-0) 

There is no call to `EncryptedKeyShare::is_valid`, no pairing check, and no verification that the G1/G2 points satisfy the vetKD relation. A Byzantine node can sign arbitrary valid-curve-point bytes with its legitimate node key and the share will be admitted to the validated pool.

**Step 2 — VetKD threshold-stop optimization is explicitly disabled.**

For ECDSA/Schnorr, `inputs_already_have_enough_shares` stops validating once `f+1` shares are collected. For VetKD this guard unconditionally returns `false`:

```rust
ThresholdSigInputs::VetKd(_inputs) => return false,
``` [2](#0-1) 

The comment says "The worst that can happen is to validate a few extra shares." In practice this means all n shares from all nodes — including the Byzantine node's crafted share — are admitted to the validated pool and passed to `combine_encrypted_key_shares`.

**Step 3 — `combine_all` fails on one bad share; fallback is O(n·pairing).**

`combine_all` interpolates all shares and checks the combined result with a single `is_valid` call (3 pairings). One cryptographically invalid share corrupts the interpolation, so `is_valid` fails and `InvalidShares` is returned: [3](#0-2) 

The caller in `combine_encrypted_key_shares_internal` then falls back to `combine_valid_shares`: [4](#0-3) 

`combine_valid_shares` calls `EncryptedKeyShare::is_valid` on every share individually. Each call performs 2 multi-pairings (4 G1/G2 pairings total via `Gt::multipairing`): [5](#0-4) 

For a 34-node subnet this is ≈68 pairings per request vs. 3 pairings on the happy path — roughly a 23× increase in cryptographic work per vetKD combine operation.

**Step 4 — The fallback is confirmed by existing tests.**

The integration test `should_succeed_if_reconstruction_threshold_many_shares_are_valid` explicitly demonstrates that swapping c1/c3 bytes (structurally valid, cryptographically invalid) causes `combine_all` to fail and the fallback to be logged: [6](#0-5) 

---

### Impact Explanation

Every vetKD key derivation request processed by the combining replica incurs ~23× the expected pairing cost as long as the Byzantine node participates. BLS12-381 pairings are among the most expensive operations in the crypto stack. Sustained triggering degrades the throughput of the vetKD signing queue, causing queued requests to back up and response latency to grow. The system remains correct (it still produces valid keys) but availability of the vetKD service is constrained.

---

### Likelihood Explanation

Any single compromised subnet node (below the reconstruction threshold) can execute this attack with no special tooling: generate three random valid BLS12-381 curve points, serialize them as an `EncryptedKeyShare`, sign the bytes with the node's legitimate signing key, and broadcast. The share passes every admission check. The attack is persistent across all future vetKD requests until the node is removed via governance.

---

### Recommendation

Add a cryptographic validity check inside `verify_encrypted_key_share_internal`. After the basic signature passes, deserialize the share and call `EncryptedKeyShare::is_valid` against the node's public key (derivable from the transcript data already in the store). Shares that fail this check should be rejected with a new `VetKdKeyShareVerificationError::InvalidShareContent` variant and removed from the unvalidated pool, preventing them from ever reaching `combine_encrypted_key_shares`. This moves the O(pairing) cost to the per-share validation step (which is already parallelized) and eliminates the O(n·pairing) fallback trigger.

---

### Proof of Concept

```
1. Byzantine node B is a legitimate subnet member (below threshold).
2. B generates three random valid G1/G2 points: c1 ∈ G1, c2 ∈ G2, c3 ∈ G1.
3. B serializes them as an EncryptedKeyShare (structurally valid, passes deserialization).
4. B signs the raw bytes with its node signing key → node_signature is valid.
5. B broadcasts VetKdKeyShare { signer_id: B, share: { encrypted_key_share, node_signature } }.
6. Receiving replicas call verify_encrypted_key_share → basic sig check passes → share admitted to validated pool.
7. combine_encrypted_key_shares_internal is called with n shares including B's.
8. combine_all interpolates all shares → combined result fails is_valid → returns InvalidShares.
9. Fallback: combine_valid_shares iterates all n shares, calls is_valid per share (2 multi-pairings each).
10. Result: correct key is produced, but at ~23× the pairing cost.
11. Repeat for every vetKD request → sustained throughput degradation.
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

**File:** rs/consensus/idkg/src/signer.rs (L576-580)
```rust
            // VetKd's API does not expose the number of shares needed for reconstruction directly.
            // As this code path is an optimization, we conservatively assume that we do not have
            // enough shares if the inputs are for VetKd.
            // The worst thing that can happen is to validate a few extra shares.
            ThresholdSigInputs::VetKd(_inputs) => return false,
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

**File:** rs/crypto/internal/crypto_lib/bls12_381/vetkd/src/lib.rs (L259-265)
```rust
    ) -> Result<Self, EncryptedKeyCombinationError> {
        let c = Self::combine_unchecked(nodes, reconstruction_threshold)?;
        if c.is_valid(master_pk, context, input, tpk) {
            Ok(c)
        } else {
            Err(EncryptedKeyCombinationError::InvalidShares)
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
