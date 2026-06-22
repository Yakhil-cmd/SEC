### Title
Missing Validation of Presig Constant Term for Identity Point in Threshold ECDSA Signing - (File: rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/signing/ecdsa.rs)

### Summary
The `derive_rho` function in the IC threshold ECDSA signing implementation does not validate that the presignature constant term (`pre_sig`) is not the identity point (point at infinity) before using it in the signing computation. If `pre_sig` is the identity point, the signing nonce `k` becomes fully computable from public inputs, enabling private key recovery from any signature produced with that presig. This is a direct structural analog to the AZTEC finding: a degenerate MPC output that is detectable from public values is not rejected, and its use leaks the secret.

### Finding Description
In `derive_rho` at lines 29–69 of `rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/signing/ecdsa.rs`, the `pre_sig` is extracted from the presig transcript's combined commitment constant term:

```rust
let pre_sig = match &presig_transcript.combined_commitment {
    CombinedCommitment::BySummation(PolynomialCommitment::Simple(c)) => c.constant_term(),
    _ => return Err(CanisterThresholdError::UnexpectedCommitmentType),
};
```

The only validation performed is a curve-type check:

```rust
if pre_sig.curve_type() != curve_type {
    return Err(CanisterThresholdError::UnexpectedCommitmentType);
}
```

There is **no check** that `pre_sig` is not the identity point (`pre_sig.is_infinity()`). The function then computes:

```rust
let randomizer = ro.output_scalar(curve_type)?;
let randomized_pre_sig =
    pre_sig.add_points(&EccPoint::generator_g(curve_type).scalar_mul(&randomizer)?)?;
let rho = ecdsa_conversion_function(&randomized_pre_sig)?;
```

If `pre_sig = identity`, then `randomized_pre_sig = randomizer * G`. The `randomizer` is the output of a random oracle whose inputs are all public:
- `randomness` — the nonce from `ThresholdEcdsaSigInputs`, shared with all nodes
- `hashed_message` — the message hash, public
- `pre_sig` — the identity point, observable in the public transcript
- `key_tweak` — derived from the public derivation path and the public key transcript constant term

Therefore, when `pre_sig = identity`, any observer can compute `randomizer` and thus knows `k = randomizer` (the discrete log of `randomized_pre_sig`). From any resulting signature `(r, s)`, the private key is recoverable as `x = (s·k − e) / r mod order`.

The `verify` function does check `self.r.is_zero() || self.s.is_zero()` but does **not** detect the case where `r` is non-zero yet `k` is publicly known. The signing proceeds to completion and returns a valid-looking signature that leaks the key. [1](#0-0) 

The presig constant term is the sum of all dealer commitments' constant terms, computed in `IDkgTranscriptInternal::new` for the `RandomUnmasked` operation: [2](#0-1) 

This sum is a public value in the `IDkgTranscript` shared across all nodes and observable by any canister that receives the transcript.

### Impact Explanation
If the presig constant term is the identity point, the signing nonce `k` is fully determined by public information. An attacker who observes a threshold ECDSA signature `(r, s)` can:
1. Compute `randomizer` from the public random oracle inputs
2. Recover the private key: `x = (s · randomizer − e) / r mod order`

For ckBTC and ckETH, this means the subnet's threshold ECDSA private key is compromised. The attacker can sign arbitrary Bitcoin or Ethereum transactions, draining all chain-fusion funds. The impact is equivalent to a complete key compromise of the chain-fusion signing key. [3](#0-2) 

### Likelihood Explanation
The probability of the presig constant term being the identity point by chance is approximately `1/group_order ≈ 1/2^256`, which is negligible in practice. However, the check is structurally absent. The AZTEC report makes the identical argument: "While this event is extremely unlikely, failure to detect it would lead to a complete failure of the AZTEC system." A threshold of colluding dealers could also force this condition, but that requires subnet-majority corruption. The primary concern is the missing defensive check — the degenerate case is detectable from public values (the presig transcript is public) and is not rejected, mirroring the AZTEC pattern exactly.

### Recommendation
**Short term**: Add an explicit check in `derive_rho` that rejects the identity point:

```rust
if pre_sig.is_infinity()? {
    return Err(CanisterThresholdError::InvalidArguments(
        "presig constant term is the identity point".to_string()
    ));
}
```

This is the direct analog of the AZTEC recommendation to check whether Boneh-Boyen signatures equal 1.

**Long term**: Validate all cryptographically sensitive transcript parameters — including the key transcript constant term and all intermediate commitment evaluations — for degenerate values before use in signing operations. [4](#0-3) 

### Proof of Concept
1. Observe the presig transcript for a pending threshold ECDSA signing request. The constant term `pre_sig = presig_transcript.combined_commitment.commitment().constant_term()` is public.
2. Check `pre_sig.is_infinity()`. If true, proceed.
3. Reconstruct `randomizer` by feeding the public inputs into the same random oracle: `randomness` (from the signing request nonce), `hashed_message`, `pre_sig = identity`, `key_tweak = derive_tweak(derivation_path, key_transcript.constant_term())`.
4. Wait for the subnet to produce the signature `(r, s)`.
5. Compute `k = randomizer` (the discrete log of `randomized_pre_sig = randomizer * G`).
6. Recover the private key: `x = (s · k − e) · r⁻¹ mod order`, where `e = hash_to_integer(hashed_message)`.
7. Use `x` to sign arbitrary Bitcoin or Ethereum transactions. [3](#0-2) [5](#0-4)

### Citations

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/signing/ecdsa.rs (L29-69)
```rust
fn derive_rho(
    curve_type: EccCurveType,
    hashed_message: &[u8],
    randomness: &Randomness,
    derivation_path: &DerivationPath,
    key_transcript: &IDkgTranscriptInternal,
    presig_transcript: &IDkgTranscriptInternal,
) -> CanisterThresholdResult<(EccScalar, EccScalar, EccScalar, EccPoint)> {
    let pre_sig = match &presig_transcript.combined_commitment {
        // Presignatures should be always RandomUnmasked
        CombinedCommitment::BySummation(PolynomialCommitment::Simple(c)) => c.constant_term(),
        _ => return Err(CanisterThresholdError::UnexpectedCommitmentType),
    };

    if pre_sig.curve_type() != curve_type {
        return Err(CanisterThresholdError::UnexpectedCommitmentType);
    }

    let (key_tweak, _chain_key) = derivation_path.derive_tweak(&key_transcript.constant_term())?;

    let alg = match curve_type {
        EccCurveType::K256 => IdkgProtocolAlgorithm::EcdsaSecp256k1,
        EccCurveType::P256 => IdkgProtocolAlgorithm::EcdsaSecp256r1,
        _ => return Err(CanisterThresholdError::CurveMismatch),
    };

    let mut ro = RandomOracle::new(DomainSep::RerandomizePresig(alg));
    ro.add_bytestring("randomness", &randomness.get())?;
    ro.add_bytestring("hashed_message", hashed_message)?;
    ro.add_point("pre_sig", &pre_sig)?;
    ro.add_scalar("key_tweak", &key_tweak)?;
    let randomizer = ro.output_scalar(curve_type)?;

    // Rerandomize presignature
    let randomized_pre_sig =
        pre_sig.add_points(&EccPoint::generator_g(curve_type).scalar_mul(&randomizer)?)?;

    let rho = ecdsa_conversion_function(&randomized_pre_sig)?;

    Ok((rho, key_tweak, randomizer, randomized_pre_sig))
}
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/signing/ecdsa.rs (L378-380)
```rust
        if self.r.is_zero() || self.s.is_zero() {
            return Err(CanisterThresholdError::InvalidSignature);
        }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/idkg/transcript.rs (L248-264)
```rust
            IDkgTranscriptOperationInternal::RandomUnmasked => {
                // Combine commitments via sum
                let mut combined = vec![EccPoint::identity(curve); reconstruction_threshold];

                for dealing in verified_dealings.values() {
                    if dealing.commitment.ctype() != PolynomialCommitmentType::Simple {
                        return Err(CanisterThresholdError::UnexpectedCommitmentType);
                    }

                    let c = dealing.commitment.points();
                    for i in 0..reconstruction_threshold {
                        combined[i] = combined[i].add_points(&c[i])?;
                    }
                }

                CombinedCommitment::BySummation(SimpleCommitment::new(combined).into())
            }
```
