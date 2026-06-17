### Title
Blake2f Precompile (EIP-152) Not Registered in Production Build Causes EVM Semantic Mismatch — (`evm_interpreter/src/precompile_addresses.rs`, `system_hooks/src/lib.rs`)

---

### Summary

ZKsync OS claims EVM equivalence but the Blake2f precompile (address `0x0009`, EIP-152, part of Ethereum since Istanbul) is not registered in the default production build. Calls to address `0x0009` fall through to regular empty-contract execution, returning success with zero return bytes instead of the expected 64-byte hash output. Any contract relying on Blake2f for cryptographic commitments, proof verification, or hash-based access control receives silently wrong results.

---

### Finding Description

`BLAKE2F_HOOK_ADDRESS_LOW` (`0x0009`) is conditionally compiled only under the `eip-152` or `mock-unsupported-precompiles` feature flags: [1](#0-0) 

In `add_precompiles()`, the only path that registers anything at address `0x0009` is the `mock-unsupported-precompiles` guard: [2](#0-1) 

The mock implementation is explicitly labeled "Not to be used in production" and writes **zero bytes** to the output buffer regardless of input: [3](#0-2) 

A real, correct `Blake2FPrecompile` implementation exists in `basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_152/impls.rs` and is registered via `initialize_eip_152`, but only under `#[cfg(feature = "eip-152")]`: [4](#0-3) 

Without the `eip-152` feature, address `0x0009` is absent from `PRECOMPILE_ADDRESSES_LOWS`: [5](#0-4) 

This means a call to `0x0009` is dispatched as a regular call to an empty account — it succeeds and returns empty data — rather than executing the Blake2f compression function and returning 64 bytes.

---

### Impact Explanation

**EVM semantic mismatch / valid-execution unprovability / funds-loss path.**

On Ethereum mainnet, `CALL` to `0x0009` with valid 213-byte input returns 64 bytes of Blake2f output. On ZKsync OS (production, no `eip-152` feature), the same call returns 0 bytes with success. Contracts that:

- Use Blake2f as a hash commitment (e.g., Zcash-compatible bridges, Filecoin-style proofs)
- Check `returndatasize()` after the call and branch on it
- Use the 64-byte output as a key or proof element

…will silently receive wrong data. A contract that gates fund withdrawal on a Blake2f-based proof check would accept any input (since the precompile always "succeeds" with empty output, and a naive `require(success)` passes), enabling unauthorized fund extraction.

---

### Likelihood Explanation

Blake2f has been a standard Ethereum precompile since the Istanbul hard fork (December 2019). Any EVM-equivalent L2 is expected to support it. An attacker who deploys or interacts with a Blake2f-dependent contract (bridges, ZK verifiers, Zcash-compatible protocols) on ZKsync OS can trigger the mismatch with a normal unprivileged transaction. No special privileges, oracle manipulation, or governance access is required — only a `CALL` to address `0x0009`.

---

### Recommendation

Enable the `eip-152` feature in the production build and call `initialize_eip_152` unconditionally alongside the other precompile registrations in `add_precompiles()`. The correct implementation already exists in `basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_152/impls.rs`; it only needs to be wired into the default precompile set. The mock in `mock_precompiles.rs` should remain test-only and never be compiled into a production binary. [6](#0-5) 

---

### Proof of Concept

1. Deploy the following contract on ZKsync OS (production build, no `eip-152` feature):

```solidity
contract Blake2fCheck {
    function check() external returns (bool ok, uint256 retSize) {
        // Valid 213-byte Blake2f input: 4 rounds, known state, message, counters, flag
        bytes memory input = new bytes(213);
        input[3] = 0x04; // 4 rounds (big-endian u32)
        // ... fill remaining 209 bytes with zeros (valid per EIP-152)
        (ok,) = address(0x09).call(input);
        assembly { retSize := returndatasize() }
    }
}
```

2. Call `check()`. On Ethereum mainnet: `ok = true`, `retSize = 64`. On ZKsync OS production: `ok = true`, `retSize = 0`.

3. A contract that does `require(retSize == 64)` before using the output as a proof element will revert on ZKsync OS, breaking cross-chain compatibility. A contract that does only `require(ok)` and then reads 64 bytes of return data will read zeros — silently accepting any Blake2f "proof."

### Citations

**File:** evm_interpreter/src/precompile_addresses.rs (L9-10)
```rust
#[cfg(any(feature = "eip-152", feature = "mock-unsupported-precompiles"))]
pub const BLAKE2F_HOOK_ADDRESS_LOW: u16 = 0x0009;
```

**File:** evm_interpreter/src/precompile_addresses.rs (L28-45)
```rust
pub const PRECOMPILE_ADDRESSES_LOWS: &[u16] = &[
    ECRECOVER_HOOK_ADDRESS_LOW,
    SHA256_HOOK_ADDRESS_LOW,
    RIPEMD160_HOOK_ADDRESS_LOW,
    ID_HOOK_ADDRESS_LOW,
    MODEXP_HOOK_ADDRESS_LOW,
    ECADD_HOOK_ADDRESS_LOW,
    ECMUL_HOOK_ADDRESS_LOW,
    ECPAIRING_HOOK_ADDRESS_LOW,
    #[cfg(any(feature = "eip-152", feature = "mock-unsupported-precompiles"))]
    BLAKE2F_HOOK_ADDRESS_LOW,
    #[cfg(any(
        feature = "point_eval_precompile",
        feature = "mock-unsupported-precompiles"
    ))]
    POINT_EVAL_HOOK_ADDRESS_LOW,
    #[cfg(feature = "p256_precompile")]
    P256_VERIFY_PREHASH_HOOK_ADDRESS_LOW,
```

**File:** system_hooks/src/lib.rs (L169-177)
```rust
    #[cfg(feature = "mock-unsupported-precompiles")]
    {
        add_precompile::<
            _,
            _,
            crate::call_hooks::mock_precompiles::mock_precompiles::Blake2f,
            MissingSystemFunctionErrors,
        >(hooks, BLAKE2F_HOOK_ADDRESS_LOW)?;

```

**File:** system_hooks/src/call_hooks/mock_precompiles.rs (L1-28)
```rust
//! Mocked precompiles needed to pass some tests in the EVM test suite.
//! Not to be used in production.
#[allow(clippy::module_inception)]
#[cfg(feature = "mock-unsupported-precompiles")]
pub(crate) mod mock_precompiles {
    use zk_ee::{
        common_traits::TryExtend,
        interface_error,
        system::{
            base_system_functions::MissingSystemFunctionErrors, errors::subsystem::SubsystemError,
            MockedSystemFunctionError, Resources, SystemFunction,
        },
    };

    pub struct Blake2f;
    impl<R: Resources> SystemFunction<R, MissingSystemFunctionErrors> for Blake2f {
        fn execute<D: TryExtend<u8> + ?Sized, A: core::alloc::Allocator + Clone>(
            input: &[u8],
            _output: &mut D,
            _resources: &mut R,
            _allocator: A,
        ) -> Result<(), SubsystemError<MissingSystemFunctionErrors>> {
            if input.len() != 213 {
                return Err(interface_error!(MockedSystemFunctionError::InvalidInputLength).into());
            }
            Ok(())
        }
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_152/mod.rs (L26-36)
```rust
pub fn initialize_eip_152<S: EthereumLikeTypes>(
    hooks_storage: &mut HooksStorage<S, S::Allocator>,
) -> Result<(), InternalError>
where
    S::IO: IOSubsystemExt,
{
    add_precompile::<S, S::Allocator, Blake2FPrecompile, Blake2FPrecompileErrors>(
        hooks_storage,
        BLAKE_HOOK_ADDRESS_LOW,
    )
}
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_152/impls.rs (L48-60)
```rust
pub struct Blake2FPrecompile;

impl<R: Resources> SystemFunction<R, Blake2FPrecompileErrors> for Blake2FPrecompile {
    fn execute<
        D: zk_ee::common_traits::TryExtend<u8> + ?Sized,
        A: core::alloc::Allocator + Clone,
    >(
        input: &[u8],
        output: &mut D,
        resources: &mut R,
        _allocator: A,
    ) -> Result<(), zk_ee::system::errors::subsystem::SubsystemError<Blake2FPrecompileErrors>> {
        if input.len() != INPUT_LEN {
```
