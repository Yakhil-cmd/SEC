### Title
EIP-2537 BLS12-381 Precompiles Charge Zero Native Resources, Allowing Prover-Cost Exhaustion Without Compensation - (File: `basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/`)

---

### Summary

All seven EIP-2537 BLS12-381 precompiles in ZKsync OS hardcode `cost_native = 0`, meaning they consume **no native resources** despite being among the most expensive operations to prove in ZK circuits. Because the native resource is the mechanism that limits how much proving work a block can impose on the prover, this omission allows an unprivileged caller to fill a block with BLS12-381 operations that are cheap in native resources but extremely expensive to prove, without the prover being compensated.

---

### Finding Description

ZKsync OS implements a **double resource accounting** model: EVM gas (ergs) tracks EE-level computation, and **native resources** track the off-chain proving cost ("how many RISC-V cycles it takes to prove a given computation"). The native resource limit per transaction is derived as `nativeLimit = gasLimit × nativePerGas`, and the block-level native limit (`MAX_NATIVE_COMPUTATIONAL`) caps total proving work per block.

Every EIP-2537 BLS12-381 precompile sets `cost_native = 0`:

- `Bls12381G1AdditionPrecompile` — `cost_native = 0` [1](#0-0) 
- `Bls12381G2AdditionPrecompile` — `cost_native = 0` [2](#0-1) 
- `Bls12381G1MSMPrecompile` — `cost_native = 0` [3](#0-2) 
- `Bls12381G2MSMPrecompile` — `cost_native = 0` [4](#0-3) 
- `Bls12381PairingCheckPrecompile` — `cost_native = 0` [5](#0-4) 
- `Bls12381G1MappingPrecompile` — `cost_native = 0` [6](#0-5) 
- `Bls12381G2MappingPrecompile` — `cost_native = 0` [7](#0-6) 

This is in stark contrast to the analogous BN254 precompiles, which charge substantial native costs. For example, `BN254_PAIRING_BASE_NATIVE_COST = native_with_delegations!(13_000_000, 500_000, 0)` per pair. [8](#0-7) 

BLS12-381 operations are **more** expensive to prove than BN254 operations (larger field, more complex group law), yet they charge zero native resources.

The native resource is defined as the off-chain proving cost: [9](#0-8) 

The block-level native limit enforces a cap on total proving work per block: [10](#0-9) 

Because BLS12-381 precompiles consume zero native resources, they are invisible to this cap. A transaction can loop over BLS12-381 calls until EVM gas is exhausted, but the native resource budget is never touched, so the block-level native limit is never triggered by these operations.

---

### Impact Explanation

**Impact: Medium**

The prover is forced to prove BLS12-381 operations without any native resource compensation. Because native resources are the mechanism by which the protocol ensures the prover is paid for its work, setting `cost_native = 0` for all BLS12-381 precompiles means:

1. A single transaction can exhaust the block's EVM gas budget entirely on BLS12-381 calls (e.g., ~426 pairing calls at 70,300 gas each within a 30M gas block).
2. The block's `MAX_NATIVE_COMPUTATIONAL` limit is not consumed by these calls, so the native budget appears unused even though the prover must do substantial work.
3. The prover bears the full proving cost of BLS12-381 operations without being compensated through the native resource accounting system.
4. This can cause proving latency spikes or, in extreme cases, proving failures if the prover's capacity is exceeded.

---

### Likelihood Explanation

**Likelihood: High**

- Any unprivileged user can call BLS12-381 precompiles via a standard EVM `CALL` to addresses `0x0b`–`0x11`.
- No special role, governance approval, or privileged access is required.
- The attack requires only a standard transaction with sufficient EVM gas.
- The EIP-2537 precompiles are registered and active in the production hook setup. [11](#0-10) 

---

### Recommendation

Assign non-zero native costs to all EIP-2537 BLS12-381 precompiles, calibrated to their actual ZK proving complexity. Use the same methodology applied to BN254 precompiles (benchmarking RISC-V cycle counts for each operation). At minimum:

- G1/G2 addition: assign a native cost comparable to or greater than `BN254_ECADD_NATIVE_COST`.
- G1/G2 MSM: assign a per-point native cost scaled to the number of pairs.
- Pairing check: assign a fixed + per-pair native cost comparable to or greater than `BN254_PAIRING_BASE_NATIVE_COST` / `BN254_PAIRING_PER_PAIR_NATIVE_COST`.
- Field-to-curve mappings: assign a native cost reflecting their proving complexity.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract BLS12AttackPoC {
    // BLS12-381 pairing precompile address (EIP-2537)
    address constant BLS12_PAIRING = address(0x11);

    // Valid G1 point (generator) + G2 point (generator) encoding
    // 1 pair = 192 bytes (G1: 128 bytes, G2: 256 bytes... actually per EIP-2537: G1=128, G2=256)
    // For simplicity, use zero points which are valid infinity points
    bytes constant ONE_PAIR = new bytes(384); // 128 (G1) + 256 (G2)

    function attack() external {
        // Loop until EVM gas is exhausted.
        // Each call costs ~70,300 EVM gas but ZERO native resources.
        // With 30M block gas: ~426 pairing calls, each doing a full BLS12-381 pairing.
        // Native resource budget is never touched.
        while (gasleft() > 100_000) {
            (bool ok,) = BLS12_PAIRING.call{gas: 80_000}(ONE_PAIR);
            // ok may be false for zero points, but gas is still consumed
        }
    }
}
```

**Execution path:**
1. Attacker deploys `BLS12AttackPoC` and calls `attack()` with `gas_limit = 30_000_000`.
2. Each iteration calls `Bls12381PairingCheckPrecompile::execute`, which charges `(1 * 32600 + 37700) * ERGS_PER_GAS` ergs but `cost_native = 0`. [12](#0-11) 
3. After ~426 iterations, EVM gas is exhausted. Native resources consumed: **0**.
4. The block's `MAX_NATIVE_COMPUTATIONAL` check passes trivially. [10](#0-9) 
5. The prover must prove 426 BLS12-381 pairings with no native resource compensation.

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/addition.rs (L21-26)
```rust
        let cost_ergs = Ergs(BLS12_381_G1_ADDITION_GAS * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/addition.rs (L62-67)
```rust
        let cost_ergs = Ergs(BLS12_381_G2_ADDITION_GAS * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs (L197-202)
```rust
        let cost_ergs = Ergs(cost * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs (L265-270)
```rust
        let cost_ergs = Ergs(cost * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/pairing.rs (L31-40)
```rust
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
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/mappings.rs (L26-30)
```rust
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/mappings.rs (L76-80)
```rust
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_system/src/cost_constants.rs (L47-48)
```rust
pub const BN254_PAIRING_BASE_NATIVE_COST: u64 = native_with_delegations!(13_000_000, 500_000, 0);
pub const BN254_PAIRING_PER_PAIR_NATIVE_COST: u64 = BN254_PAIRING_BASE_NATIVE_COST;
```

**File:** docs/double_resource_accounting.md (L17-18)
```markdown
The native resource models the offchain cost of processing a transaction. Currently, this is dominated by proving and publishing data. A good intuition for it is "how many RISC-V cycles it takes to prove a given computation".

```

**File:** basic_bootloader/src/bootloader/block_flow/zk/mod.rs (L68-77)
```rust
    } else if !cfg!(feature = "resources_for_tester")
        && computational_native_used > MAX_NATIVE_COMPUTATIONAL
    {
        // ZKsync OS-specific resources are not checked for evm tester
        system_log!(
            system,
            "Block native limit reached, invalidating transaction\n"
        );
        Err(InvalidTransaction::BlockNativeLimitReached)
    } else if !cfg!(feature = "resources_for_tester") && pubdata_used > system.get_pubdata_limit() {
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/mod.rs (L37-71)
```rust
pub fn initialize_eip_2537<S: EthereumLikeTypes>(
    hooks: &mut HooksStorage<S, S::Allocator>,
) -> Result<(), InternalError>
where
    S::IO: IOSubsystemExt,
{
    add_precompile::<S, S::Allocator, Bls12381G1AdditionPrecompile, Bls12PrecompileErrors>(
        hooks,
        BLS12_G1ADD,
    )?;
    add_precompile::<S, S::Allocator, Bls12381G2AdditionPrecompile, Bls12PrecompileErrors>(
        hooks,
        BLS12_G2ADD,
    )?;
    add_precompile::<S, S::Allocator, Bls12381G1MSMPrecompile, Bls12PrecompileErrors>(
        hooks,
        BLS12_G1MSM,
    )?;
    add_precompile::<S, S::Allocator, Bls12381G2MSMPrecompile, Bls12PrecompileErrors>(
        hooks,
        BLS12_G2MSM,
    )?;
    add_precompile::<S, S::Allocator, Bls12381PairingCheckPrecompile, Bls12PrecompileErrors>(
        hooks,
        BLS12_PAIRING_CHECK,
    )?;
    add_precompile::<S, S::Allocator, Bls12381G1MappingPrecompile, Bls12PrecompileErrors>(
        hooks,
        BLS12_MAP_FP_TO_G1,
    )?;
    add_precompile::<S, S::Allocator, Bls12381G2MappingPrecompile, Bls12PrecompileErrors>(
        hooks,
        BLS12_MAP_FP2_TO_G2,
    )?;
    Ok(())
```
