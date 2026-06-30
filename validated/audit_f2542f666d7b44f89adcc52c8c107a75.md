### Title
Off-by-One Infinity Flag Byte Placement in `g1_add` and `g2_add` NEAR Host Function Input Encoding Causes EIP-2537 Precompiles to Reject Valid Infinity Point Inputs - (File: `engine-sdk/src/bls12_381/contract.rs`)

### Summary
In the on-chain (NEAR contract) implementation of the BLS12-381 `g1_add` and `g2_add` precompiles, the infinity-point flag byte is written to the wrong byte offset in the NEAR host function input buffer. When either input point is the point at infinity, the flag `0x40` is placed at index `1` (for point 0) or index `98`/`194` (for point 1) instead of the correct positions `0` and `97`/`193`. The NEAR host function then interprets the input as a non-infinity point whose first coordinate byte is `0x40`, which exceeds the BLS12-381 field modulus and is therefore an invalid field element. The host function returns an error, and the precompile propagates `ERR_BLS12_ELEMENT_NOT_IN_G1` / `ERR_BLS12_ELEMENT_NOT_IN_G2` to the EVM caller. This is a concrete deviation from EIP-2537, which mandates that the infinity point is a valid input to both operations.

### Finding Description
The NEAR `bls12381_p1_sum` host function encodes each G1 point as **1 flag byte + 96 coordinate bytes = 97 bytes per point**. For a two-point sum the 194-byte buffer layout must be:

```
[0]      flag for point 0  (0x00 = normal, 0x40 = infinity)
[1..97]  x_0 || y_0
[97]     flag for point 1
[98..194] x_1 || y_1
```

In `g1_add` (`engine-sdk/src/bls12_381/contract.rs`, lines 69–83):

```rust
let mut g1_input = [0u8; 4 * FP_LENGTH + 2];   // 194 bytes, zero-initialised

// Point 0 – infinity branch
g1_input[1] |= 0x40;          // BUG: flag written to index 1, must be index 0

// Point 1 – infinity branch
g1_input[2 + 2 * FP_LENGTH] |= 0x40;  // BUG: 2+96 = 98; must be 1+96 = 97
```

Because the buffer is zero-initialised, the non-infinity branches write coordinates at the correct offsets (`[1..97]` and `[98..194]`) and the implicit flag bytes at indices `0` and `97` remain `0x00` — so **non-infinity inputs work correctly**. Only the infinity branches are broken.

When the infinity flag is placed at index `1`, the NEAR host function reads:
- `flag[0] = 0x00` → treats the point as non-infinity
- `x_0[0] = 0x40` → first byte of the x-coordinate is `0x40`

`0x40 || 0x00…00` (48 bytes) is larger than the BLS12-381 field modulus (`0x1a01…`), so the host function rejects it as an invalid field element and returns a non-zero error code. The wrapper maps any non-zero code to `Bls12381Error::ElementNotInG1`, which surfaces to the EVM as `ExitError::Other("ERR_BLS12_ELEMENT_NOT_IN_G1")`.

The identical off-by-one exists in `g2_add` (lines 126–141) for `bls12381_p2_sum`.

**Contrast with sibling functions:** `g1_msm` (line 103), `g2_msm` (line 162), and `pairing_check` (lines 212, 222) all correctly write the flag at `g_input[offset]` — the first byte of each point's slot — confirming this is an isolated encoding mistake in the two `_add` functions.

**Contrast with standalone implementation:** `engine-sdk/src/bls12_381/standalone.rs` uses `blst` directly and handles the infinity point correctly. The bug is therefore invisible to any test that exercises only the standalone path and only manifests on Aurora mainnet (where the `contract` feature is compiled in).

### Impact Explanation
Any EVM smart contract deployed on Aurora that calls the `g1_add` precompile (address `0x000…0B`) or `g2_add` precompile (address `0x000…0D`) with the point at infinity as either operand will receive a revert instead of the correct result. EIP-2537 explicitly permits the infinity point as a valid input; Ethereum mainnet returns the other operand unchanged. Smart contracts that rely on this identity-element property — for example, contracts that aggregate BLS public keys or signatures where the neutral element can appear as an intermediate value — will malfunction on Aurora. If such a contract guards withdrawal or redemption logic, affected users cannot access their funds until the bug is patched and the contract is updated, constituting a temporary freezing of funds.

### Likelihood Explanation
The infinity point arises naturally in BLS key-aggregation schemes (e.g., when a participant's contribution cancels out, or when a signer set is empty). Any Aurora-deployed contract that ports Ethereum BLS aggregation logic and exercises this code path will trigger the bug deterministically. The bug is silent in all existing tests because the test suite runs against the standalone implementation. The likelihood is moderate: the Prague hard fork (which introduced these precompiles) is recent, but BLS-based protocols are actively being deployed.

### Recommendation
In `g1_add`, change:
```rust
// Point 0 infinity
g1_input[1] |= 0x40;          // wrong
// →
g1_input[0] |= 0x40;          // correct

// Point 1 infinity
g1_input[2 + 2 * FP_LENGTH] |= 0x40;   // wrong (index 98)
// →
g1_input[1 + 2 * FP_LENGTH] |= 0x40;   // correct (index 97)
```

Apply the same fix in `g2_add`:
```rust
// Point 0 infinity
g2_input[1] |= 0x40;          // wrong
// →
g2_input[0] |= 0x40;          // correct

// Point 1 infinity
g2_input[2 + 4 * FP_LENGTH] |= 0x40;   // wrong (index 194)
// →
g2_input[1 + 4 * FP_LENGTH] |= 0x40;   // correct (index 193)
```

Add contract-mode integration tests that pass the infinity point (256 bytes of `0x00` for G1, 512 bytes of `0x00` for G2) and assert the output equals the other operand, mirroring the EIP-2537 test vectors.

### Proof of Concept
Call the `g1_add` precompile at address `0x000…0B` with 256 bytes of zeros (both inputs are the infinity point). Per EIP-2537 the expected output is 128 bytes of zeros (the infinity point). On Ethereum mainnet this succeeds. On Aurora mainnet the call reverts with `ERR_BLS12_ELEMENT_NOT_IN_G1` because:

1. `g1_input[0]` remains `0x00` (flag = non-infinity — wrong).
2. `g1_input[1]` is set to `0x40` (treated as the first byte of x-coordinate).
3. `bls12381_p1_sum` sees x = `0x40 || 0x00…` > field modulus → returns error code ≠ 0.
4. `exports::bls12381_p1_sum` returns `Err(error_code)`.
5. `g1_add` maps this to `Bls12381Error::ElementNotInG1` → EVM revert.

The same sequence applies to `g2_add` with 512 bytes of zeros. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** engine-sdk/src/bls12_381/contract.rs (L69-87)
```rust
    let mut g1_input = [0u8; 4 * FP_LENGTH + 2];

    if input[..G1_INPUT_ITEM_LENGTH] == [0; G1_INPUT_ITEM_LENGTH] {
        g1_input[1] |= 0x40;
    } else {
        g1_input[1..1 + FP_LENGTH].copy_from_slice(p0_x);
        g1_input[1 + FP_LENGTH..1 + 2 * FP_LENGTH].copy_from_slice(p0_y);
    }

    if input[G1_INPUT_ITEM_LENGTH..] == [0; G1_INPUT_ITEM_LENGTH] {
        g1_input[2 + 2 * FP_LENGTH] |= 0x40;
    } else {
        g1_input[2 + 2 * FP_LENGTH..2 + 3 * FP_LENGTH].copy_from_slice(p1_x);
        g1_input[2 + 3 * FP_LENGTH..2 + 4 * FP_LENGTH].copy_from_slice(p1_y);
    }

    let output =
        exports::bls12381_p1_sum(&g1_input[..]).map_err(|_| Bls12381Error::ElementNotInG1)?;
    Ok(padding_g1_result(&output))
```

**File:** engine-sdk/src/bls12_381/contract.rs (L121-145)
```rust
#[allow(clippy::range_plus_one)]
pub fn g2_add(input: &[u8]) -> Result<Vec<u8>, Bls12381Error> {
    let (p0_x, p0_y) = extract_g2(&input[..G2_INPUT_ITEM_LENGTH])?;
    let (p1_x, p1_y) = extract_g2(&input[G2_INPUT_ITEM_LENGTH..])?;

    let mut g2_input = [0u8; 8 * FP_LENGTH + 2];

    // Check zero input
    if input[..G2_INPUT_ITEM_LENGTH] == [0; G2_INPUT_ITEM_LENGTH] {
        g2_input[1] |= 0x40;
    } else {
        g2_input[1..1 + 2 * FP_LENGTH].copy_from_slice(&p0_x);
        g2_input[1 + 2 * FP_LENGTH..1 + 4 * FP_LENGTH].copy_from_slice(&p0_y);
    }

    if input[G2_INPUT_ITEM_LENGTH..] == [0; G2_INPUT_ITEM_LENGTH] {
        g2_input[2 + 4 * FP_LENGTH] |= 0x40;
    } else {
        g2_input[2 + 4 * FP_LENGTH..2 + 6 * FP_LENGTH].copy_from_slice(&p1_x);
        g2_input[2 + 6 * FP_LENGTH..2 + 8 * FP_LENGTH].copy_from_slice(&p1_y);
    }

    let output =
        exports::bls12381_p2_sum(&g2_input[..]).map_err(|_| Bls12381Error::ElementNotInG2)?;
    Ok(padding_g2_result(&output))
```

**File:** engine-sdk/src/bls12_381/contract/exports.rs (L3-15)
```rust
pub fn bls12381_p1_sum(input: &[u8]) -> Result<[u8; 96], u64> {
    unsafe {
        const REGISTER_ID: u64 = 1;
        let error_code =
            exports::bls12381_p1_sum(input.len() as u64, input.as_ptr() as u64, REGISTER_ID);
        if error_code != 0 {
            return Err(error_code);
        }
        let mut bytes = [0u8; 96];
        exports::read_register(REGISTER_ID, bytes.as_mut_ptr() as u64);
        Ok(bytes)
    }
}
```

**File:** engine-sdk/src/bls12_381/mod.rs (L1-9)
```rust
#[cfg(feature = "contract")]
mod contract;
#[cfg(not(feature = "contract"))]
mod standalone;

#[cfg(feature = "contract")]
pub use contract::{g1_add, g1_msm, g2_add, g2_msm, map_fp_to_g1, map_fp2_to_g2, pairing_check};
#[cfg(not(feature = "contract"))]
pub use standalone::{g1_add, g1_msm, g2_add, g2_msm, map_fp_to_g1, map_fp2_to_g2, pairing_check};
```
