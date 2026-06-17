### Title
Missing EIP-2537 (BLS12-381) and EIP-152 (Blake2F) Precompile Registrations in ZK Block Flow — (`basic_bootloader/src/bootloader/block_flow/zk/post_init_op.rs`)

---

### Summary

The `ZKHeaderPostInitOp::post_init_op` function, which initializes the system hooks for the ZK (production proving) execution path, omits the registration of the BLS12-381 (EIP-2537) and Blake2F (EIP-152) precompiles. The Ethereum block flow counterpart (`EthereumPostInitOp`) correctly registers both. This creates an EVM semantic mismatch: in the ZK execution path, calls to these precompile addresses silently succeed with empty return data instead of executing the cryptographic operation.

---

### Finding Description

The `EthereumPostInitOp` registers all three groups of hooks:

```rust
// basic_bootloader/src/bootloader/block_flow/ethereum/post_init_op.rs
fn post_init_op(...) {
    add_precompiles(system_functions)?;
    hooks::eip_2537::initialize_eip_2537(system_functions)?;  // BLS12-381
    hooks::eip_152::initialize_eip_152(system_functions)?;    // Blake2F
    Ok(())
}
```

The `ZKHeaderPostInitOp` only calls `add_precompiles` and the ZKsync-specific system hooks, but **never calls `initialize_eip_2537` or `initialize_eip_152`**:

```rust
// basic_bootloader/src/bootloader/block_flow/zk/post_init_op.rs
fn post_init_op(...) {
    system_hooks::add_precompiles(system_functions)?;
    // ZKsync-specific hooks...
    // initialize_eip_2537 and initialize_eip_152 are ABSENT
    Ok(())
}
```

The `add_precompiles` function in `system_hooks/src/lib.rs` only adds Blake2F under the `mock-unsupported-precompiles` feature flag (lines 169–184), which is not a production feature. BLS12-381 is never added there at all. The `HooksStorage` lookup for an unregistered address falls through to treating the callee as an empty account — returning success with empty returndata.

---

### Impact Explanation

Any smart contract deployed on ZKsync OS that calls the Blake2F precompile (`0x0009`) or any BLS12-381 precompile (EIP-2537 addresses) in the ZK execution path will receive an empty successful return instead of the correct cryptographic output. This silently corrupts the result of cryptographic operations (hash functions, elliptic curve operations) without reverting. Contracts relying on these precompiles for signature verification, zero-knowledge proof verification, or other cryptographic checks will behave incorrectly — potentially allowing forged proofs or bypassed authentication — with no on-chain error signal.

---

### Likelihood Explanation

Any unprivileged transaction sender can trigger this by calling a contract that internally invokes `staticcall` or `call` to address `0x0009` (Blake2F) or any BLS12-381 address. No special privilege is required. The ZK path is the production execution path for ZKsync OS, so this affects all real block execution.

---

### Recommendation

Add the missing registrations to `ZKHeaderPostInitOp::post_init_op` in `basic_bootloader/src/bootloader/block_flow/zk/post_init_op.rs`, mirroring the Ethereum flow:

```rust
fn post_init_op(...) {
    system_hooks::add_precompiles(system_functions)?;
    // existing ZKsync-specific hooks...
    // Add missing precompile registrations:
    hooks::eip_2537::initialize_eip_2537(system_functions)?;
    hooks::eip_152::initialize_eip_152(system_functions)?;
    Ok(())
}
```

---

### Proof of Concept

1. Deploy a contract on ZKsync OS that calls the Blake2F precompile at `0x0009` with valid EIP-152 input and checks that the return data is non-empty.
2. Execute the block through the ZK path (`ZKHeaderPostInitOp`).
3. Observe: the call returns success with **empty** returndata, because no hook is registered for `0x0009` in the ZK flow — the address is treated as an empty account.
4. Execute the same block through the Ethereum path (`EthereumPostInitOp`).
5. Observe: the call returns the correct 64-byte Blake2F output.

The divergence is directly caused by the absent `initialize_eip_152` call in `ZKHeaderPostInitOp`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/post_init_op.rs (L1-20)
```rust
use super::*;
use system_hooks::add_precompiles;
use zk_ee::system::errors::internal::InternalError;

impl<S: EthereumLikeTypes> PostSystemInitOp<S> for EthereumPostInitOp
where
    S::IO: IOSubsystemExt,
{
    fn post_init_op<Config: BasicBootloaderExecutionConfig>(
        _system: &mut System<S>,
        system_functions: &mut HooksStorage<S, <S as SystemTypes>::Allocator>,
    ) -> Result<(), InternalError> {
        add_precompiles(system_functions)?;

        hooks::eip_2537::initialize_eip_2537(system_functions)?;
        hooks::eip_152::initialize_eip_152(system_functions)?;

        Ok(())
    }
}
```

**File:** basic_bootloader/src/bootloader/block_flow/zk/post_init_op.rs (L1-28)
```rust
use super::*;
use zk_ee::system::errors::internal::InternalError;

impl<S: EthereumLikeTypes> PostSystemInitOp<S> for ZKHeaderPostInitOp
where
    S::IO: IOSubsystemExt,
{
    fn post_init_op<Config: BasicBootloaderExecutionConfig>(
        _system: &mut System<S>,
        system_functions: &mut HooksStorage<S, <S as SystemTypes>::Allocator>,
    ) -> Result<(), InternalError> {
        system_hooks::add_precompiles(system_functions)?;

        #[cfg(not(feature = "disable_system_contracts"))]
        {
            system_hooks::add_l1_messenger(system_functions)?;
            system_hooks::add_set_bytecode_on_address_hook(system_functions)?;
            system_hooks::add_contract_deployer(system_functions)?;
            system_hooks::add_interop_root_reporter(system_functions)?;
            system_hooks::add_system_context_reporter(system_functions)?;

            // TODO(EVM-1191): temporary solution, should be removed before the release
            system_hooks::add_base_token_mint(system_functions)?;
        }

        Ok(())
    }
}
```

**File:** system_hooks/src/lib.rs (L125-204)
```rust
pub fn add_precompiles<S: EthereumLikeTypes, A: Allocator + Clone>(
    hooks: &mut HooksStorage<S, A>,
) -> Result<(), InternalError>
where
    S::IO: IOSubsystemExt,
{
    add_precompile::<
        _,
        _,
        <S::SystemFunctions as SystemFunctions<_>>::Secp256k1ECRecover,
        Secp256k1ECRecoverErrors,
    >(hooks, ECRECOVER_HOOK_ADDRESS_LOW)?;
    add_precompile::<_, _, <S::SystemFunctions as SystemFunctions<_>>::Sha256, Sha256Errors>(
        hooks,
        SHA256_HOOK_ADDRESS_LOW,
    )?;
    add_precompile::<_, _, <S::SystemFunctions as SystemFunctions<_>>::RipeMd160, RipeMd160Errors>(
        hooks,
        RIPEMD160_HOOK_ADDRESS_LOW,
    )?;
    add_precompile::<_, _, IdentityPrecompile, IdentityPrecompileErrors>(
        hooks,
        ID_HOOK_ADDRESS_LOW,
    )?;
    add_precompile_ext::<
        _,
        _,
        <S::SystemFunctionsExt as SystemFunctionsExt<_>>::ModExp,
        ModExpErrors,
    >(hooks, MODEXP_HOOK_ADDRESS_LOW)?;
    add_precompile::<_, _, <S::SystemFunctions as SystemFunctions<_>>::Bn254Add, Bn254AddErrors>(
        hooks,
        ECADD_HOOK_ADDRESS_LOW,
    )?;
    add_precompile::<_, _, <S::SystemFunctions as SystemFunctions<_>>::Bn254Mul, Bn254MulErrors>(
        hooks,
        ECMUL_HOOK_ADDRESS_LOW,
    )?;
    add_precompile::<
        _,
        _,
        <S::SystemFunctions as SystemFunctions<_>>::Bn254PairingCheck,
        Bn254PairingCheckErrors,
    >(hooks, ECPAIRING_HOOK_ADDRESS_LOW)?;
    #[cfg(feature = "mock-unsupported-precompiles")]
    {
        add_precompile::<
            _,
            _,
            crate::call_hooks::mock_precompiles::mock_precompiles::Blake2f,
            MissingSystemFunctionErrors,
        >(hooks, BLAKE2F_HOOK_ADDRESS_LOW)?;

        #[cfg(not(feature = "point_eval_precompile"))]
        add_precompile::<
            _,
            _,
            crate::call_hooks::mock_precompiles::mock_precompiles::PointEvaluation,
            MissingSystemFunctionErrors,
        >(hooks, POINT_EVAL_HOOK_ADDRESS_LOW)?;
    }
    #[cfg(feature = "point_eval_precompile")]
    add_precompile::<
        _,
        _,
        <S::SystemFunctions as SystemFunctions<_>>::PointEvaluation,
        PointEvaluationErrors,
    >(hooks, POINT_EVAL_HOOK_ADDRESS_LOW)?;

    #[cfg(feature = "p256_precompile")]
    {
        add_precompile::<
            _,
            _,
            <S::SystemFunctions as SystemFunctions<_>>::P256Verify,
            P256VerifyErrors,
        >(hooks, P256_VERIFY_PREHASH_HOOK_ADDRESS_LOW)?;
    }
    Ok(())
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

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/mod.rs (L1-30)
```rust
use crypto::ark_ec::AffineRepr;
use system_hooks::add_precompile;
use zk_ee::common_structs::system_hooks::HooksStorage;
use zk_ee::interface_error;

define_subsystem!(Bls12Precompile,
  interface Bls12PrecompileInterfaceError
  {
      InvalidFieldElement,
      InvalidG1Point,
      InvalidG2Point,
      InvalidInputSize,
      PointNotInSubgroup,
  }
);

use evm_interpreter::ERGS_PER_GAS;

use crypto::ark_ff::PrimeField;
use crypto::bls12_381::*;
use zk_ee::define_subsystem;
use zk_ee::system::errors::internal::InternalError;
use zk_ee::system::{EthereumLikeTypes, IOSubsystemExt};

mod addition;
mod addresses;
mod mappings;
mod msm;
mod pairing;

```
