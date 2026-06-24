Audit Report

## Title
Off-by-One in Derivation Path Length Validation Causes Signing Failure at Maximum Allowed Path Length — (`rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/signing/key_derivation.rs`)

## Summary
The management canister's Candid layer accepts up to 255 user-supplied derivation path elements, but the internal crypto layer enforces a 255-element limit on the *total* path after prepending the caller's principal. A canister supplying exactly 255 elements — the documented maximum — produces an internal path of length 256, which `derive_tweak_with_chain_code` rejects with `InvalidArguments`. The API contract is violated: the outer layer permits what the inner layer rejects.

## Finding Description
**Layer 1 — Candid gate accepts 0–255 user elements:**
`DerivationPath` is typed as `BoundedVec<MAXIMUM_DERIVATION_PATH_LENGTH, UNBOUNDED, UNBOUNDED, ByteBuf>` where `MAXIMUM_DERIVATION_PATH_LENGTH = 255` ( [1](#0-0) , [2](#0-1) ). The deserializer rejects only when `elements.len() >= MAX_ALLOWED_LEN`, meaning it successfully accepts exactly 255 elements before rejecting the 256th ( [3](#0-2) ).

**Layer 2 — Principal prepend inflates path to 256:**
`DerivationPath::from(ExtendedDerivationPath)` unconditionally prepends the caller's principal as the first element, making the internal path length `1 + user_path_length` ( [4](#0-3) ). With 255 user elements, the internal length becomes 256.

**Layer 3 — Internal limit rejects 256:**
`derive_tweak_with_chain_code` enforces `self.len() > MAXIMUM_DERIVATION_PATH_LENGTH` where `MAXIMUM_DERIVATION_PATH_LENGTH = 255` ( [5](#0-4) , [6](#0-5) ). `256 > 255` is true, so the call returns `CanisterThresholdError::InvalidArguments`.

**Existing test does not catch this:**
`verify_bip32_extended_key_derivation_max_length_enforced` tests raw `DerivationPath::new_bip32` paths of length 0–255 directly, without going through `From<ExtendedDerivationPath>` and without prepending a principal ( [7](#0-6) ). It therefore never exercises the 256-element case that arises in production.

**Execution path:** `sign_with_ecdsa` → `ecdsa_sign_share_internal` calls `DerivationPath::from(derivation_path)` then `tecdsa_sign_share` → `derive_tweak_with_chain_code` → `InvalidArguments` error propagated back to the canister as a rejection. The same path applies to `derive_threshold_public_key` via `DerivationPath::from(extended_derivation_path)`.

## Impact Explanation
Any canister invoking `sign_with_ecdsa` or `ecdsa_public_key` with exactly 255 derivation path elements will always receive an error response. This is a permanent, deterministic denial of service for any canister relying on the documented maximum path length for key isolation or business logic — including potential Chain Fusion or ckToken integrations that use deep key hierarchies. This matches the allowed impact: **Application/platform-level DoS with concrete user and protocol harm** (High, $2,000–$10,000).

## Likelihood Explanation
The trigger requires supplying exactly 255 path elements, which is an edge case but one explicitly permitted and documented by the management canister API. No privileged access is required — any canister can reach this path with a single inter-canister call. The failure is deterministic and repeatable: every call with 255 elements will fail, with no workaround available to the calling canister short of reducing its path length below the documented maximum.

## Recommendation
Reduce the management canister's Candid-layer limit from 255 to 254 (i.e., `MAXIMUM_DERIVATION_PATH_LENGTH - 1`) so the internal path after principal prepending never exceeds 255 elements. Alternatively, raise the internal `MAXIMUM_DERIVATION_PATH_LENGTH` in `key_derivation.rs` to 256. Whichever fix is chosen, add an integration test that calls `sign_with_ecdsa` through `ExtendedDerivationPath` with exactly `MAXIMUM_DERIVATION_PATH_LENGTH` user-supplied elements and asserts success, to prevent regression.

## Proof of Concept
```rust
// State-machine / PocketIC test
let path = vec![vec![0u8; 1]; 255]; // exactly MAXIMUM_DERIVATION_PATH_LENGTH elements
let args = SignWithECDSAArgs {
    message_hash: [1u8; 32],
    derivation_path: DerivationPath::new(
        path.iter().map(|v| ByteBuf::from(v.clone())).collect()
    ),
    key_id: EcdsaKeyId { curve: EcdsaCurve::Secp256k1, name: "test_key".to_string() },
};
// Step 1: Candid decode succeeds — 255 <= 255, BoundedVec accepts it
// Step 2: DerivationPath::from(ExtendedDerivationPath) prepends caller → internal length = 256
// Step 3: derive_tweak_with_chain_code: 256 > 255 → InvalidArguments
// Observed: canister receives rejection instead of signature
// Expected: canister receives valid signature (per documented API)
```
A unit-level reproduction can be written directly against `derive_tweak_with_chain_code` by constructing a `DerivationPath` with 256 elements and asserting `Err(CanisterThresholdError::InvalidArguments(_))`, then constructing one via `From<ExtendedDerivationPath>` with a 255-element user path and asserting `Ok(_)` — which will fail, confirming the bug.

### Citations

**File:** rs/types/management_canister_types/src/lib.rs (L60-60)
```rust
const MAXIMUM_DERIVATION_PATH_LENGTH: usize = 255;
```

**File:** rs/types/management_canister_types/src/lib.rs (L3236-3236)
```rust
pub type DerivationPath = BoundedVec<MAXIMUM_DERIVATION_PATH_LENGTH, UNBOUNDED, UNBOUNDED, ByteBuf>;
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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/src/lib.rs (L689-700)
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

**File:** rs/crypto/internal/crypto_lib/threshold_sig/canister_threshold_sig/tests/key_derivation.rs (L25-28)
```rust
    for i in 0..=255 {
        let path = vec![i as u32; i];
        assert_matches!(setup.public_key(&DerivationPath::new_bip32(&path)), Ok(_));
    }
```
