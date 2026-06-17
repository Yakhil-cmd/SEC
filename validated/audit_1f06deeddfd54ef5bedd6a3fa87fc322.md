### Title
BLS12-381 Pairing Precompile Incorrectly Rejects Empty Input, Diverging from EIP-2537 Specification - (File: `basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/pairing.rs`)

---

### Summary

The `Bls12381PairingCheckPrecompile` rejects empty calldata with an `InvalidInputSize` error. Per EIP-2537, an empty input to the pairing check precompile is valid and **must** return `true` (the multiplicative identity of the target field). The same incorrect early-exit pattern also affects `Bls12381G1MSMPrecompile` and `Bls12381G2MSMPrecompile`, which should return the point at infinity for empty input. This is a direct EVM semantic mismatch reachable by any unprivileged transaction sender.

---

### Finding Description

In `pairing.rs`, the `execute` function for `Bls12381PairingCheckPrecompile` begins with:

```rust
if input.len() == 0 {
    return Err(interface_error!(
        Bls12PrecompileInterfaceError::InvalidInputSize
    ));
}
``` [1](#0-0) 

This causes the precompile to revert on empty input. However, EIP-2537 explicitly specifies that an empty input to the pairing check precompile is valid and must return `0x01` (true), because the empty product of pairings equals the multiplicative identity `1` in `Fq12`.

The same incorrect pattern exists in both MSM precompiles:

```rust
// G1MSM
if input.len() == 0 {
    return Err(interface_error!(Bls12PrecompileInterfaceError::InvalidInputSize));
}
// G2MSM
if input.len() == 0 {
    return Err(interface_error!(Bls12PrecompileInterfaceError::InvalidInputSize));
}
``` [2](#0-1) [3](#0-2) 

Per EIP-2537, G1MSM and G2MSM with empty input must return the point at infinity (identity element), not an error.

The correct behavior is already implemented in the analogous BN254 pairing check:

```rust
let success = if src.is_empty() {
    true
} else {
    bn254_pairing_check_inner::<A>(num_pairs, src, allocator)
        .map_err(|_| interface_error!(Bn254PairingCheckInterfaceError::InvalidPoint))?
};
``` [4](#0-3) 

The BN254 implementation correctly returns `true` for empty input. The BLS12-381 implementation does not, despite the same mathematical identity applying.

The `FIELD_ELEMENT_SERIALIZATION_LEN`, `G1_SERIALIZATION_LEN`, and `G2_SERIALIZATION_LEN` constants are correctly defined:

```rust
const SCALAR_SERIALIZATION_LEN: usize = 32;
const FIELD_ELEMENT_SERIALIZATION_LEN: usize = 64;
const FIELD_EXT_ELEMENT_SERIALIZATION_LEN: usize = FIELD_ELEMENT_SERIALIZATION_LEN * 2;
const G1_SERIALIZATION_LEN: usize = FIELD_ELEMENT_SERIALIZATION_LEN * 2;
const G2_SERIALIZATION_LEN: usize = FIELD_EXT_ELEMENT_SERIALIZATION_LEN * 2;
``` [5](#0-4) 

The bug is not in the size constants but in the missing identity-case handling for empty input — an analog to the pyUmbral report's "hardcoded assumption that doesn't hold for all valid inputs."

---

### Impact Explanation

**Type:** EVM semantic mismatch / crypto precompile parsing bug.

Any smart contract that calls the BLS12-381 pairing precompile (EIP-2537 address `0x0f`) with empty calldata — a valid operation per the EIP — will receive a revert instead of `true`. This breaks:

1. **Contracts using conditional pairing checks** where the pair list may be empty (e.g., batch verification with zero elements).
2. **EVM equivalence**: ZKsync OS diverges from Ethereum mainnet behavior for this precompile, violating the stated EVM equivalence goal.
3. **Forward/proving divergence risk**: If the forward (x86) and proving (RISC-V) paths handle the error differently, a valid-execution-unprovability scenario could arise.

The same impact applies to G1MSM and G2MSM with empty input: contracts expecting the identity point will receive a revert.

---

### Likelihood Explanation

**High.** The entry path requires only a standard EVM `CALL` to the BLS12-381 pairing precompile address with zero-length calldata. No privileged access, no oracle manipulation, no special conditions. Any deployed contract that uses BLS12-381 pairing in a loop that may have zero iterations will trigger this. The EIP-2537 specification is unambiguous on this point, and the BN254 implementation in the same codebase demonstrates the developers knew the correct pattern.

---

### Recommendation

1. In `Bls12381PairingCheckPrecompile::execute`, replace the empty-input rejection with the identity return, mirroring the BN254 implementation:

```rust
if input.is_empty() {
    // Empty input: pairing product over empty set = 1 (identity)
    output.try_extend([0u8; 31]).map_err(|_| out_of_return_memory!())?;
    output.try_extend([1u8]).map_err(|_| out_of_return_memory!())?;
    return Ok(());
}
```

2. In `Bls12381G1MSMPrecompile::execute` and `Bls12381G2MSMPrecompile::execute`, replace the empty-input rejection with a return of the respective point at infinity (128 or 256 zero bytes).

3. Add test cases for empty-input behavior for all three BLS12-381 precompiles.

---

### Proof of Concept

An attacker (or any contract) sends a transaction calling the BLS12-381 pairing precompile at EIP-2537 address `0x0f` with empty calldata:

```
PUSH0          // push 0 (retSize)
PUSH0          // push 0 (retOffset)
PUSH0          // push 0 (argsSize = 0, empty input)
PUSH0          // push 0 (argsOffset)
PUSH0          // push 0 (value)
PUSH 0x0f      // BLS12_PAIRING_CHECK address
PUSH <gas>
CALL
```

**Expected (per EIP-2537):** Call succeeds, returns `0x0000...0001` (true).

**Actual (ZKsync OS):** Call reverts with `InvalidInputSize` error. [6](#0-5) 

The divergence from the BN254 pairing check's correct empty-input handling confirms this is an implementation oversight, not an intentional design choice. [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/pairing.rs (L26-46)
```rust
        if input.len() == 0 {
            return Err(interface_error!(
                Bls12PrecompileInterfaceError::InvalidInputSize
            ));
        }
        let num_pairs = input.len() / BLS12_381_PAIR_LEN;
        let cost_ergs = Ergs(
            ((num_pairs as u64) * BLS12_381_PAIRING_PER_PAIR_GAS + BLS12_381_PAIRING_FIXED_GAS)
                * ERGS_PER_GAS,
        );
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;

        if !input.len().is_multiple_of(BLS12_381_PAIR_LEN) {
            return Err(interface_error!(
                Bls12PrecompileInterfaceError::InvalidInputSize
            ));
        }
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs (L186-190)
```rust
        if input.len() == 0 {
            return Err(interface_error!(
                Bls12PrecompileInterfaceError::InvalidInputSize
            ));
        }
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs (L254-258)
```rust
        if input.len() == 0 {
            return Err(interface_error!(
                Bls12PrecompileInterfaceError::InvalidInputSize
            ));
        }
```

**File:** basic_system/src/system_functions/bn254_pairing_check.rs (L34-56)
```rust
            let num_pairs = src.len() / 192;
            let ergs_cost = BN254_PAIRING_STATIC_COST_ERGS
                + BN254_PAIRING_COST_PER_PAIR_ERGS.times(num_pairs as u64);
            let native_cost = (num_pairs as u64) * BN254_PAIRING_PER_PAIR_NATIVE_COST
                + BN254_PAIRING_BASE_NATIVE_COST;

            resources.charge(&R::from_ergs_and_native(
                ergs_cost,
                <R::Native as zk_ee::system::Computational>::from_computational(native_cost),
            ))?;

            if !src.len().is_multiple_of(192) {
                return Err(interface_error!(
                    Bn254PairingCheckInterfaceError::InvalidPairingSize
                ));
            }

            let success = if src.is_empty() {
                true
            } else {
                bn254_pairing_check_inner::<A>(num_pairs, src, allocator)
                    .map_err(|_| interface_error!(Bn254PairingCheckInterfaceError::InvalidPoint))?
            };
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/mod.rs (L74-78)
```rust
const SCALAR_SERIALIZATION_LEN: usize = 32;
const FIELD_ELEMENT_SERIALIZATION_LEN: usize = 64;
const FIELD_EXT_ELEMENT_SERIALIZATION_LEN: usize = FIELD_ELEMENT_SERIALIZATION_LEN * 2;
const G1_SERIALIZATION_LEN: usize = FIELD_ELEMENT_SERIALIZATION_LEN * 2;
const G2_SERIALIZATION_LEN: usize = FIELD_EXT_ELEMENT_SERIALIZATION_LEN * 2;
```
