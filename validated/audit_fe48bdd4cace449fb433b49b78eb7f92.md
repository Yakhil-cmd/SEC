Now I have all the information needed for a complete analysis. Let me trace the full attack path.

**Key findings from the code:**

1. `check_instance` only validates non-emptiness and length equality — no identity/subgroup checks on any G1/G2 input. [1](#0-0) 

2. `G1Affine::deserialize` uses `from_compressed` which verifies prime-order subgroup membership — but the identity point IS a valid prime-order subgroup element and deserializes successfully (confirmed by the `INFINITY` test vector). [2](#0-1) [3](#0-2) 

3. The three verification equations in `verify_sharing`: [4](#0-3) 

4. `verify_zk_proofs` calls both `verify_chunking` and `verify_sharing` sequentially: [5](#0-4) 

5. `combined_r` and `combined_ciphertexts` are computed from the actual ciphertext chunks via `g1_from_big_endian_chunks` — if all chunks are identity, the combined values are identity: [6](#0-5) 

6. `verify_resharing_dealing` has an additional guard: the constant term of `public_coefficients` must equal the dealer's individual public key from the resharing transcript: [7](#0-6) 

---

**Mathematical analysis of the forged proof:**

Set all instance inputs to identity: `R = identity`, `C_i = identity`, `A_k = identity`. Set proof values: `F = identity`, `A = g2^{z_α}`, `Y = g1^{z_α}`, `z_r = 0`, `z_α = anything`.

- **Thread 1**: `identity^x' + identity = g1^0` → `identity = identity` ✓
- **Thread 2**: `identity^{...} * g2^{z_α} = g2^{z_α}` → `g2^{z_α} = g2^{z_α}` ✓
- **Thread 3** (rearranged form): `identity * identity = g1^{z_α} * (g1^{z_α})^{-1}` → `identity = identity` ✓

All three equations pass regardless of `x'`.

**Can the attacker commit to arbitrary `public_coefficients`?** No. Thread 2 with non-identity `A_k` requires finding `A` such that `A = g2^{z_α} * P^{-hash(x, identity, A, g1^{z_α})}` — a fixed-point over a hash, computationally infeasible. The attack is strictly limited to `public_coefficients = identity` (the zero polynomial).

**Is the chunking proof a blocker?** No. With all randomizer and ciphertext chunks set to identity, the chunking proof can be created honestly with witness `r_j = 0, s_ij = 0` (since `y_i^0 * g1^0 = identity`).

**Is resharing blocked?** Yes. `verify_resharing_dealing` checks that `public_coefficients[0] == individual_public_key(dealer)`. The identity point won't match any legitimate dealer key.

---

### Title
Sharing Proof Forgeable for Zero Polynomial via Identity-Point Instance — (`rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/nizk_sharing.rs`)

### Summary
`check_instance` performs no identity-point or subgroup-membership check on `combined_randomizer`, `combined_ciphertexts`, or `public_coefficients`. A Byzantine dealer can construct a `SharingInstance` with all G1/G2 inputs set to the identity point and craft a `ProofSharing` that satisfies all three verification equations in `verify_sharing` without knowing any valid witness. The attack is limited to committing to the zero polynomial and is blocked for resharing DKG.

### Finding Description
`check_instance` only validates:
- `public_keys` and `public_coefficients` are non-empty
- `public_keys.len() == combined_ciphertexts.len()`

It performs no check that `combined_randomizer`, `combined_ciphertexts`, or `public_coefficients` are non-identity. The identity point is a valid prime-order subgroup element and passes `G1Affine::deserialize` / `G2Affine::deserialize` successfully.

With all instance inputs set to identity and proof values `(F=identity, A=g2^{z_α}, Y=g1^{z_α}, z_r=0, z_α=arbitrary)`, all three verification equations in `verify_sharing` reduce to `identity = identity` and pass unconditionally, regardless of the Fiat-Shamir challenge `x'`.

The full dealing path (`verify_dealing` → `verify_zk_proofs` → `verify_chunking` + `verify_sharing`) can be traversed because:
- The chunking proof is satisfiable with honest witness `r_j=0, s_ij=0` when all ciphertext chunks are identity
- `g1_from_big_endian_chunks` of all-identity chunks yields identity, producing `combined_r = identity` and `combined_ciphertexts = identity`
- `public_coefficients = identity` (G2 identity) deserializes successfully

### Impact Explanation
A single Byzantine dealer node (protocol peer below the consensus fault threshold) can submit a fresh NI-DKG dealing that passes all verification checks in `verify_dealing` while committing to the zero polynomial. The dealing is accepted into the DKG pool and can be included in the transcript. The dealer's contribution to the combined threshold key is zero, equivalent to the dealer not contributing. The NI-DKG protocol tolerates malicious dealers, so the resulting threshold key remains secure as long as enough honest dealers contribute. The attack is **not** applicable to resharing DKG (blocked by the `public_coefficients[0] == dealer_public_key` check in `verify_resharing_dealing`). The attacker **cannot** commit to an arbitrary non-zero polynomial — Thread 2 with non-identity `A_k` requires solving a hash fixed-point, which is computationally infeasible.

The concrete broken invariant is: a passing `verify_sharing` no longer guarantees knowledge of a valid witness `(r, s_1..s_n)`. The proof-of-knowledge property of the Sigma protocol is violated for the degenerate zero-polynomial case.

### Likelihood Explanation
Requires a registered dealer node acting maliciously. A single such node is sufficient. The attack is deterministic and requires no brute force.

### Recommendation
Add identity-point checks in `check_instance`:

```rust
pub fn check_instance(&self) -> Result<(), ZkProofSharingError> {
    if self.public_keys.is_empty() || self.public_coefficients.is_empty() {
        return Err(ZkProofSharingError::InvalidInstance);
    }
    if self.public_keys.len() != self.combined_ciphertexts.len() {
        return Err(ZkProofSharingError::InvalidInstance);
    }
    if self.combined_randomizer.is_identity() {
        return Err(ZkProofSharingError::InvalidInstance);
    }
    for c in &self.combined_ciphertexts {
        if c.is_identity() {
            return Err(ZkProofSharingError::InvalidInstance);
        }
    }
    for a in &self.public_coefficients {
        if a.is_identity() {
            return Err(ZkProofSharingError::InvalidInstance);
        }
    }
    Ok(())
}
```

Similarly harden `ChunkingInstance::check_instance` in `nizk_chunking.rs`.

### Proof of Concept

```rust
#[test]
fn forge_sharing_proof_with_identity_randomizer() {
    use ic_crypto_internal_bls12_381_type::{G1Affine, G2Affine, Scalar};
    use nizk_sharing::{ProofSharing, SharingInstance, verify_sharing};

    let n = 1;
    let pk = vec![G1Affine::generator().clone()]; // real receiver key (doesn't matter)
    let aa = vec![G2Affine::identity().clone()];  // zero polynomial commitment
    let rr = G1Affine::identity().clone();         // identity randomizer
    let cc = vec![G1Affine::identity().clone()];  // identity ciphertext

    let instance = SharingInstance::new(pk, aa, rr, cc);

    // Choose any z_alpha
    let z_alpha = Scalar::from_u64(42);
    let g1 = G1Affine::generator();
    let g2 = G2Affine::generator();

    let proof = ProofSharing::new(
        G1Affine::identity().clone(),                    // F = identity
        G2Affine::from(g2 * &z_alpha),                  // A = g2^z_alpha
        G1Affine::from(g1 * &z_alpha),                  // Y = g1^z_alpha
        Scalar::zero(),                                  // z_r = 0
        z_alpha,
    );

    assert_eq!(verify_sharing(&instance, &proof), Ok(()));
}
```

### Citations

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/nizk_sharing.rs (L148-156)
```rust
    pub fn check_instance(&self) -> Result<(), ZkProofSharingError> {
        if self.public_keys.is_empty() || self.public_coefficients.is_empty() {
            return Err(ZkProofSharingError::InvalidInstance);
        };
        if self.public_keys.len() != self.combined_ciphertexts.len() {
            return Err(ZkProofSharingError::InvalidInstance);
        };
        Ok(())
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/fs_ni_dkg/nizk_sharing.rs (L270-347)
```rust
    // Thread 1
    {
        // First verification equation
        // R^x' * F == g_1^z_r
        let lhs = &instance.combined_randomizer.mul_vartime(&x_challenge) + &first_move.blinder_g1;
        let rhs = instance.g1_gen.mul_vartime(&nizk.z_r);
        if lhs != rhs {
            return Err(ZkProofSharingError::InvalidProof);
        }
    }

    // Thread 2
    {
        // Second verification equation
        //   ( ∏_{k=0}^{t-1} A_k^{ Σ_{i=1}^n (i^k * x^i) } )^{x'} * A
        //     == g_2^{z_α}

        // We initialize ik with x_challenge (A) to avoid the point/scalar multiplication later
        let mut ik = vec![x_challenge.clone(); instance.public_keys.len()];

        let mut scalars = Vec::with_capacity(instance.public_coefficients.len());
        for _pc in &instance.public_coefficients {
            let acc = Scalar::muln_vartime(&ik, &xpow);
            scalars.push(acc);

            for i in 0..ik.len() {
                ik[i] *= Scalar::from_u64((i + 1) as u64);
            }
        }
        let lhs =
            G2Projective::muln_affine_vartime(&instance.public_coefficients[..], &scalars[..])
                + &nizk.aa;

        let rhs = instance.g2_gen.mul_vartime(&nizk.z_alpha);

        if lhs != rhs {
            return Err(ZkProofSharingError::InvalidProof);
        }
    }

    // Thread 3
    {
        // Third verification equation
        // Original relation:
        //   (∏_{i=1}^n C_i^{x^i})^{x'} * Y  ==  (∏_{i=1}^n y_i^{x^i})^{z_r} * g_1^{z_α}
        //
        // Equivalently, we can rewrite it by moving terms to opposite sides:
        //
        //   lhs = (∏_{i=1}^n C_i^{x^i})^{x'} * (∏_{i=1}^n y_i^{x^i})^{-z_r}
        //   rhs = g_1^{z_α} * Y^{-1}

        // The two expressions are re-arranged so that it becomes possible to compute
        // everything with a single multi scalar multiplication.

        let instance_inputs: Vec<_> = instance
            .combined_ciphertexts
            .iter()
            .chain(&instance.public_keys)
            .collect();
        let challenges = {
            let mut c = Vec::with_capacity(xpow.len() * 2);
            for xp in &xpow {
                c.push(xp * &x_challenge);
            }
            let z_r_neg = nizk.z_r.neg();
            for xp in &xpow {
                c.push(xp * &z_r_neg);
            }
            c
        };

        let lhs = G1Projective::muln_affine_vartime_ref(&instance_inputs, &challenges);
        let rhs = &instance.g1_gen.mul_vartime(&nizk.z_alpha) + &nizk.yy.neg();

        if lhs != rhs {
            return Err(ZkProofSharingError::InvalidProof);
        }
    }
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/type/src/lib.rs (L1176-1186)
```rust
            /// Deserialize a point (compressed format only)
            ///
            /// This version verifies that the decoded point is within the prime order
            /// subgroup, and is safe to call on untrusted inputs.
            pub fn deserialize<B: AsRef<[u8]>>(bytes: &B) -> Result<Self, PairingInvalidPoint> {
                let bytes : &[u8; Self::BYTES] = bytes.as_ref()
                    .try_into()
                    .map_err(|_| PairingInvalidPoint::InvalidPoint)?;
                let pt = ic_bls12_381::$affine::from_compressed(bytes);
                ctoption_ok_or!(pt, PairingInvalidPoint::InvalidPoint)
            }
```

**File:** rs/crypto/internal/crypto_lib/bls12_381/type/tests/tests.rs (L760-792)
```rust
    /// The additive identity, also known as zero.
    const INFINITY: &str = "c00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000";
    /// Powers of 2: `g1_generator * {1, 2, 4, 8, ...}`
    const POWERS_OF_2: &[&str] = &[
        GENERATOR,
        "a572cbea904d67468808c8eb50a9450c9721db309128012543902d0ac358a62ae28f75bb8f1c7c42c39a8c5529bf0f4e",
        "ac9b60d5afcbd5663a8a44b7c5a02f19e9a77ab0a35bd65809bb5c67ec582c897feb04decc694b13e08587f3ff9b5b60",
        "a85ae765588126f5e860d019c0e26235f567a9c0c0b2d8ff30f3e8d436b1082596e5e7462d20f5be3764fd473e57f9cf",
        "a73eb991aa22cdb794da6fcde55a427f0a4df5a4a70de23a988b5e5fc8c4d844f66d990273267a54dd21579b7ba6a086",
        "a72841987e4f219d54f2b6a9eac5fe6e78704644753c3579e776a3691bc123743f8c63770ed0f72a71e9e964dbf58f43",
    ];
    /// Positive numbers: `g1_generator * {1,2,3,4,...}`
    const POSITIVE_NUMBERS: &[&str] = &[
        GENERATOR,
        "a572cbea904d67468808c8eb50a9450c9721db309128012543902d0ac358a62ae28f75bb8f1c7c42c39a8c5529bf0f4e",
        "89ece308f9d1f0131765212deca99697b112d61f9be9a5f1f3780a51335b3ff981747a0b2ca2179b96d2c0c9024e5224",
        "ac9b60d5afcbd5663a8a44b7c5a02f19e9a77ab0a35bd65809bb5c67ec582c897feb04decc694b13e08587f3ff9b5b60",
        "b0e7791fb972fe014159aa33a98622da3cdc98ff707965e536d8636b5fcc5ac7a91a8c46e59a00dca575af0f18fb13dc",
    ];
    /// Negative numbers: `g1_generator * {-1, -2, -3, -4, ...}`
    const NEGATIVE_NUMBERS: &[&str] = &[
        "b7f1d3a73197d7942695638c4fa9ac0fc3688c4f9774b905a14e3a3f171bac586c55e83ff97a1aeffb3af00adb22c6bb",
        "8572cbea904d67468808c8eb50a9450c9721db309128012543902d0ac358a62ae28f75bb8f1c7c42c39a8c5529bf0f4e",
        "a9ece308f9d1f0131765212deca99697b112d61f9be9a5f1f3780a51335b3ff981747a0b2ca2179b96d2c0c9024e5224",
        "8c9b60d5afcbd5663a8a44b7c5a02f19e9a77ab0a35bd65809bb5c67ec582c897feb04decc694b13e08587f3ff9b5b60",
        "90e7791fb972fe014159aa33a98622da3cdc98ff707965e536d8636b5fcc5ac7a91a8c46e59a00dca575af0f18fb13dc",
    ];

    let g = G1Affine::generator();
    let identity = G1Affine::identity();

    g1_test_encoding(identity.clone(), INFINITY);
    g1_test_encoding(g.clone(), GENERATOR);
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/groth20_bls12_381/encryption.rs (L390-446)
```rust
    // Verify proof
    crypto::verify_chunking(
        &crypto::ChunkingInstance::new(
            public_keys.clone(),
            ciphertext.ciphertext_chunks().to_vec(),
            ciphertext.randomizers_r().clone(),
        ),
        &chunking_proof,
    )
    .map_err(|_| {
        let error = InvalidArgumentError {
            message: "Invalid chunking proof".to_string(),
        };
        CspDkgVerifyDealingError::InvalidDealingError(error)
    })?;

    // More conversions

    // TODO(CRP-2550) this loop can run in parallel
    let public_coefficients = public_coefficients
        .coefficients
        .iter()
        .map(G2Affine::deserialize)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| {
            CspDkgVerifyDealingError::MalformedDealingError(InvalidArgumentError {
                message: "Could not parse public coefficients".to_string(),
            })
        })?;

    let combined_r = util::g1_from_big_endian_chunks(ciphertext.randomizers_r());
    let combined_ciphertexts: Vec<_> = ciphertext
        .ciphertext_chunks()
        .iter()
        .map(|s| util::g1_from_big_endian_chunks(s))
        .collect();
    let sharing_proof = crypto::ProofSharing::deserialize(sharing_proof).ok_or_else(|| {
        CspDkgVerifyDealingError::MalformedDealingError(InvalidArgumentError {
            message: "Could not parse proof of correct sharing".to_string(),
        })
    })?;

    crypto::verify_sharing(
        &crypto::SharingInstance::new(
            public_keys,
            public_coefficients,
            combined_r,
            combined_ciphertexts,
        ),
        &sharing_proof,
    )
    .map_err(|_| {
        let error = InvalidArgumentError {
            message: "Invalid sharing proof".to_string(),
        };
        CspDkgVerifyDealingError::InvalidDealingError(error)
    })
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/groth20_bls12_381/encryption.rs (L454-466)
```rust
    pub fn g1_from_big_endian_chunks(terms: &[G1Affine]) -> G1Affine {
        let mut acc = G1Projective::identity();

        for term in terms {
            for _ in 0..16 {
                acc = acc.double();
            }

            acc += term;
        }

        acc.to_affine()
    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/bls12_381/src/ni_dkg/groth20_bls12_381/dealing.rs (L316-339)
```rust
    // Check the constant term in the public coefficient corresponds to the
    // individual public key of the dealer in the resharing instance
    let dealt_public_key = dealing
        .public_coefficients
        .coefficients
        .first()
        .expect("verify_dealing guarantees that public_coefficients.len() == threshold > 0");
    let reshared_public_key =
        individual_public_key(resharing_public_coefficients, dealer_resharing_index).map_err(
            |error| {
                let error = InvalidArgumentError {
                    message: format!("{error}"),
                };
                CspDkgVerifyDealingError::InvalidDealingError(error)
            },
        )?;
    if *dealt_public_key != reshared_public_key {
        let error = InvalidDealingError::ReshareMismatch {
            old: reshared_public_key,
            new: *dealt_public_key,
        };
        let error = InvalidArgumentError::from(error);
        return Err(CspDkgVerifyDealingError::InvalidDealingError(error));
    }
```
