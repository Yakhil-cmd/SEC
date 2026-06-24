### Title
Off-by-One in Derivation Path Length Validation Causes Signing Failure at Maximum Allowed Path Length — (`rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/signing/key_derivation.rs`)

### Summary

There is a concrete off-by-one between the management canister's Candid-layer limit (255 user-supplied elements) and the internal crypto layer's limit (255 total elements including the prepended caller principal). A canister supplying exactly 255 derivation path elements — the documented maximum — will have its `sign_with_ecdsa` / `ecdsa_public_key` request fail internally with `InvalidArguments`.

---

### Finding Description

**Layer 1 — Management canister Candid gate (allows up to 255 user elements):**

`DerivationPath` is typed as:

```rust
pub type DerivationPath = BoundedVec<MAXIMUM_DERIVATION_PATH_LENGTH, UNBOUNDED, UNBOUNDED, ByteBuf>;
// MAXIMUM_DERIVATION_PATH_LENGTH = 255
``` [1](#0-0) [2](#0-1) 

The `BoundedVec` deserializer rejects when `elements.len() >= MAX_ALLOWED_LEN`, meaning it accepts exactly 0–255 elements and rejects the 256th:

```rust
if elements.len() >= MAX_ALLOWED_LEN {
    return Err(...)
}
elements.push(element);
``` [3](#0-2) 

The existing test `verify_max_derivation_path_length` confirms that `i = 0..=255` all decode successfully: [4](#0-3) 

**Layer 2 — `DerivationPath::from(ExtendedDerivationPath)` prepends the caller principal:**

```rust
Self::new(
    std::iter::once(extended_derivation_path.caller.to_vec())
        .chain(extended_derivation_path.derivation_path)
        ...
)
``` [5](#0-4) 

This makes the internal `DerivationPath` length = **1 + user_path_length**. With 255 user elements, the internal length is **256**.

**Layer 3 — `derive_tweak_with_chain_code` rejects paths longer than 255:**

```rust
if self.len() > Self::MAXIMUM_DERIVATION_PATH_LENGTH {
    return Err(CanisterThresholdError::InvalidArguments(...));
}
``` [6](#0-5) 

`MAXIMUM_DERIVATION_PATH_LENGTH = 255` here too: [7](#0-6) 

So `256 > 255 = true` → error. The internal test `verify_bip32_extended_key_derivation_max_length_enforced` tests the raw `DerivationPath` directly (without the prepended principal), so it does not catch this off-by-one: [8](#0-7) 

**Execution path:**

The execution environment decodes `SignWithECDSAArgs` (succeeds), stores the request, then during signing `sign_share` → `ecdsa_sign_share_internal` → `tecdsa_sign_share` calls `DerivationPath::from(extended_derivation_path)` and then `derive_tweak_with_chain_code`, which returns `InvalidArguments`. The same path applies to `derive_threshold_public_key`: [9](#0-8) [10](#0-9) 

---

### Impact Explanation

Any canister that calls `sign_with_ecdsa` or `ecdsa_public_key` with exactly 255 derivation path elements (the documented maximum) will receive an error response. The API contract is violated: the management canister explicitly accepts 255 elements, but the crypto layer rejects the resulting 256-element internal path. Canisters relying on the maximum path length for key isolation or business logic will silently fail to obtain signatures.

---

### Likelihood Explanation

The trigger requires supplying exactly 255 path elements — an edge case in practice, but one that is explicitly permitted by the documented API. Any canister can reach this path with a single inter-canister call to the management canister. No privileged access is required.

---

### Recommendation

Reduce the management canister's Candid-layer limit from 255 to 254 (i.e., `MAXIMUM_DERIVATION_PATH_LENGTH - 1`) to account for the prepended caller principal, so the internal path never exceeds 255 elements. Alternatively, raise the internal `MAXIMUM_DERIVATION_PATH_LENGTH` in `derive_tweak_with_chain_code` to 256. Add an integration test that calls `sign_with_ecdsa` with exactly 255 user-supplied path elements and asserts success.

---

### Proof of Concept

```rust
// State-machine test pseudocode
let path = vec![vec![0u8; 1]; 255]; // exactly MAXIMUM_DERIVATION_PATH_LENGTH elements
let args = SignWithECDSAArgs {
    message_hash: [1u8; 32],
    derivation_path: DerivationPath::new(path.iter().map(|v| ByteBuf::from(v.clone())).collect()),
    key_id: EcdsaKeyId { curve: EcdsaCurve::Secp256k1, name: "test_key".to_string() },
};
// Candid decode succeeds (255 <= 255)
// Internal DerivationPath::from prepends caller → length = 256
// derive_tweak_with_chain_code: 256 > 255 → InvalidArguments error
// Canister receives rejection instead of signature
```

### Citations

**File:** rs/types/management_canister_types/src/lib.rs (L60-60)
```rust
const MAXIMUM_DERIVATION_PATH_LENGTH: usize = 255;
```

**File:** rs/types/management_canister_types/src/lib.rs (L3236-3236)
```rust
pub type DerivationPath = BoundedVec<MAXIMUM_DERIVATION_PATH_LENGTH, UNBOUNDED, UNBOUNDED, ByteBuf>;
```

**File:** rs/types/management_canister_types/src/lib.rs (L5351-5386)
```rust
    fn verify_max_derivation_path_length() {
        for i in 0..=MAXIMUM_DERIVATION_PATH_LENGTH {
            let path = DerivationPath::new(vec![ByteBuf::from(vec![0_u8, 32]); i]);
            let encoded = path.encode();
            assert_eq!(DerivationPath::decode(&encoded).unwrap(), path);

            let sign_with_ecdsa = SignWithECDSAArgs {
                message_hash: [1; 32],
                derivation_path: path.clone(),
                key_id: EcdsaKeyId {
                    curve: EcdsaCurve::Secp256k1,
                    name: "test".to_string(),
                },
            };

            let encoded = sign_with_ecdsa.encode();
            assert_eq!(
                SignWithECDSAArgs::decode(&encoded).unwrap(),
                sign_with_ecdsa
            );

            let ecdsa_public_key = ECDSAPublicKeyArgs {
                canister_id: None,
                derivation_path: path,
                key_id: EcdsaKeyId {
                    curve: EcdsaCurve::Secp256k1,
                    name: "test".to_string(),
                },
            };

            let encoded = ecdsa_public_key.encode();
            assert_eq!(
                ECDSAPublicKeyArgs::decode(&encoded).unwrap(),
                ecdsa_public_key
            );
        }
```

**File:** rs/types/management_canister_types/src/bounded_vec.rs (L108-113)
```rust
                while let Some(element) = seq.next_element::<T>()? {
                    if elements.len() >= MAX_ALLOWED_LEN {
                        return Err(serde::de::Error::custom(format!(
                            "The number of elements exceeds maximum allowed {MAX_ALLOWED_LEN}"
                        )));
                    }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/lib.rs (L689-701)
```rust
impl From<ExtendedDerivationPath> for DerivationPath {
    fn from(extended_derivation_path: ExtendedDerivationPath) -> Self {
        // We use generalized derivation for all path bytestrings after prepending
        // the caller's principal. It means only big-endian encoded 4-byte values
        // less than 2^31 are compatible with BIP-32 non-hardened derivation path.
        Self::new(
            std::iter::once(extended_derivation_path.caller.to_vec())
                .chain(extended_derivation_path.derivation_path)
                .map(crate::signing::key_derivation::DerivationIndex)
                .collect::<Vec<_>>(),
        )
    }
}
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/signing/key_derivation.rs (L34-34)
```rust
    pub const MAXIMUM_DERIVATION_PATH_LENGTH: usize = 255;
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/signing/key_derivation.rs (L194-200)
```rust
        if self.len() > Self::MAXIMUM_DERIVATION_PATH_LENGTH {
            return Err(CanisterThresholdError::InvalidArguments(format!(
                "Derivation path len {} larger than allowed maximum of {}",
                self.len(),
                Self::MAXIMUM_DERIVATION_PATH_LENGTH
            )));
        }
```

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/tests/key_derivation.rs (L10-38)
```rust
fn verify_bip32_extended_key_derivation_max_length_enforced() -> Result<(), CanisterThresholdError>
{
    let nodes = 3;
    let threshold = nodes / 3;

    let seed = Seed::from_bytes(b"verify_bip32_extended_key_derivation_max_length");

    let setup = EcdsaSignatureProtocolSetup::new(
        TestConfig::new(IdkgProtocolAlgorithm::EcdsaSecp256k1, EccCurveType::K256),
        nodes,
        threshold,
        threshold,
        seed,
    )?;

    for i in 0..=255 {
        let path = vec![i as u32; i];
        assert_matches!(setup.public_key(&DerivationPath::new_bip32(&path)), Ok(_));
    }

    for i in 256..1024 {
        let path = vec![i as u32; i];
        assert_matches!(
            setup.public_key(&DerivationPath::new_bip32(&path)),
            Err(CanisterThresholdError::InvalidArguments(_))
        );
    }

    Ok(())
```

**File:** rs/crypto/utils/canister_threshold_sig/src/lib.rs (L10-26)
```rust
pub fn derive_threshold_public_key(
    master_public_key: &MasterPublicKey,
    extended_derivation_path: ExtendedDerivationPath,
) -> Result<PublicKey, CanisterThresholdGetPublicKeyError> {
    ic_crypto_internal_threshold_sig_canister_threshold_sig::derive_threshold_public_key(
        master_public_key,
        &DerivationPath::from(extended_derivation_path),
    )
    .map_err(|e| match e {
        DeriveThresholdPublicKeyError::InvalidArgument(s) => {
            CanisterThresholdGetPublicKeyError::InvalidArgument(s)
        }
        DeriveThresholdPublicKeyError::InternalError(e) => {
            CanisterThresholdGetPublicKeyError::InternalError(format!("{e:?}"))
        }
    })
}
```

**File:** rs/crypto/internal/crypto_service_provider/src/vault/local_csp_vault/tecdsa/mod.rs (L77-110)
```rust
    fn ecdsa_sign_share_internal(
        &self,
        derivation_path: ExtendedDerivationPath,
        hashed_message: &[u8],
        nonce: &Randomness,
        key: &IDkgTranscriptInternal,
        kappa_unmasked: &IDkgTranscriptInternal,
        lambda_masked: &IDkgTranscriptInternal,
        kappa_times_lambda: &IDkgTranscriptInternal,
        key_times_lambda: &IDkgTranscriptInternal,
        algorithm_id: AlgorithmId,
    ) -> Result<ThresholdEcdsaSigShareInternal, ThresholdEcdsaCreateSigShareError> {
        let lambda_share =
            self.combined_commitment_opening_from_sks(&lambda_masked.combined_commitment)?;
        let kappa_times_lambda_share =
            self.combined_commitment_opening_from_sks(&kappa_times_lambda.combined_commitment)?;
        let key_times_lambda_share =
            self.combined_commitment_opening_from_sks(&key_times_lambda.combined_commitment)?;

        tecdsa_sign_share(
            &DerivationPath::from(derivation_path),
            hashed_message,
            *nonce,
            key,
            kappa_unmasked,
            &lambda_share,
            &kappa_times_lambda_share,
            &key_times_lambda_share,
            algorithm_id,
        )
        .map_err(|e| ThresholdEcdsaCreateSigShareError::InternalError {
            internal_error: format!("{e:?}"),
        })
    }
```
