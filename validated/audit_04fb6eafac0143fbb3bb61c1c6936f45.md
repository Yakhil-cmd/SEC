### Title
BLS12-381 EIP-2537 Precompiles Charge Zero Native Resources, Enabling Prover DoS via Unbounded Computation - (`basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/`)

---

### Summary

Every EIP-2537 BLS12-381 precompile implementation in ZKsync OS hardcodes `cost_native = 0`, meaning no native resources (RISC-V proving cycles) are charged regardless of how many pairs are processed. Because the block-level native limit (`MAX_NATIVE_COMPUTATIONAL`) is enforced against the accumulated native cost, an attacker can fill a block with BLS12-381 MSM or pairing calls that each do arbitrarily expensive elliptic-curve computation while the native counter stays at zero. The prover is then forced to execute unbounded work for a block that passed all resource checks, analogous to how `fulfillRandomWords()` exceeded the Chainlink VRF coordinator's hard gas cap and caused a DoS.

---

### Finding Description

ZKsync OS implements a **double resource accounting** model: every operation charges both *ergs* (EVM gas equivalent) and *native resources* (RISC-V cycle cost used to bound prover work). The native limit per transaction is derived from `gasLimit * nativePerGas`, and a block-level cap `MAX_NATIVE_COMPUTATIONAL` is enforced after each transaction.

All seven EIP-2537 precompile implementations set `cost_native = 0`:

**G1 MSM** (`msm.rs` lines 197–202):
```rust
let cost_ergs = Ergs(cost * ERGS_PER_GAS);
let cost_native = 0;
resources.charge(&R::from_ergs_and_native(
    cost_ergs,
    <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
))?;
```

**G2 MSM** (`msm.rs` lines 264–270) — identical pattern, `cost_native = 0`.

**Pairing check** (`pairing.rs` lines 36–40) — identical pattern, `cost_native = 0`.

**G1/G2 Addition** (`addition.rs` lines 22–26, 63–67) — `cost_native = 0`.

**G1/G2 Field-to-curve mapping** (`mappings.rs` lines 26–29, 76–79) — `cost_native = 0`.

The MSM and pairing operations are the most dangerous because their actual RISC-V cycle cost scales with the number of input pairs. The `msm()` function runs a full Pippenger multi-scalar multiplication loop over all pairs:

```rust
for window_idx in 0..num_windows {          // 256/c windows
    for i in 0..bases.len() {               // N pairs
        reusable_buckets[(scalar - 1) as usize] += &bases[i];
    }
    for el in reusable_buckets.iter_mut().rev() { ... }
}
```

With `num_pairs = 128` (the maximum discount-table index), G2 MSM charges `22500 * 128 * 519/1000 ≈ 1,494,720 EVM gas` but **zero native resources**. A 30 M gas block can hold ~20 such calls. Each call executes hundreds of G2 field-extension multiplications in RISC-V, yet the block-level native check at:

```rust
} else if !cfg!(feature = "resources_for_tester")
    && computational_native_used > MAX_NATIVE_COMPUTATIONAL
{
    Err(InvalidTransaction::BlockNativeLimitReached)
```

sees `computational_native_used = 0` and passes unconditionally.

---

### Impact Explanation

The prover must execute every RISC-V instruction in the block to generate a proof. If a block contains many BLS12-381 G2 MSM or pairing calls, the actual RISC-V cycle count can far exceed the prover's budget for a single block, making the block **unprovable**. This is a denial-of-service on the proving pipeline: the sequencer accepts and seals the block (all EVM gas and native checks pass), but the prover cannot finish within its cycle budget, stalling the chain. The attack is cheap for the attacker — only EVM gas is paid — and repeatable across every block.

---

### Likelihood Explanation

EIP-2537 precompiles are callable by any unprivileged EVM transaction. An attacker needs only to send a transaction with sufficient EVM gas (≈1.5 M gas per 128-pair G2 MSM call) to a contract that calls address `0x0e` (BLS12_G2MSM). No special privileges, governance access, or external oracle manipulation is required. The attack is deterministic and reproducible.

---

### Recommendation

Assign a non-zero, pair-count-proportional native cost to each BLS12-381 precompile, calibrated to the actual RISC-V cycle cost measured on the target prover. For example:

```rust
// Calibrate these constants against actual RISC-V cycle benchmarks
const G2_MSM_NATIVE_BASE: u64 = 50_000;
const G2_MSM_NATIVE_PER_PAIR: u64 = 200_000;

let cost_native = G2_MSM_NATIVE_BASE + G2_MSM_NATIVE_PER_PAIR * num_pairs as u64;
resources.charge(&R::from_ergs_and_native(
    cost_ergs,
    <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
))?;
```

Apply the same pattern to G1 MSM, pairing check, addition, and field-mapping precompiles, with constants proportional to their relative RISC-V costs.

---

### Proof of Concept

1. Deploy a contract with the following logic (pseudocode):
   ```solidity
   // Call BLS12-381 G2 MSM precompile (0x0e) with 128 valid G2 pairs
   // Each pair: 128 bytes G2 point + 32 bytes scalar = 160 bytes
   // Total input: 128 * 160 = 20480 bytes
   assembly {
       let success := staticcall(gas(), 0x0e, input_ptr, 20480, out_ptr, 256)
   }
   ```
2. Send a transaction calling this contract with `gas_limit = 2_000_000` (covers ~1.5 M EVM gas for 128-pair G2 MSM).
3. Observe: transaction succeeds, `computational_native_used` reported as 0, block-level native limit check passes.
4. Repeat 15–20 times within one block (within the block gas limit).
5. The prover receives a valid sealed block but must execute ~20 × 128 G2 MSM operations in RISC-V, far exceeding its cycle budget → block cannot be proven → chain stalls.

**Root cause lines:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

**Block-level native limit check that is bypassed:** [6](#0-5) 

**MSM inner loop whose RISC-V cost is unaccounted:** [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs (L122-155)
```rust
    #[allow(clippy::needless_range_loop)]
    for window_idx in 0..num_windows {
        let last_window = window_idx == num_windows - 1;

        unsafe {
            core::hint::assert_unchecked(bases.len() == bigints.len());
        }
        for i in 0..bases.len() {
            let bigint = &mut bigints[i];
            // get window
            let scalar: u64 = bigint.as_ref()[0] & lowest_bits_mask;

            use core::ops::ShrAssign;
            bigint.shr_assign(c as u32);

            if scalar != 0 {
                reusable_buckets[(scalar - 1) as usize] += &bases[i];
            }
        }

        // now sum over buckets
        let mut tmp = zero;
        let mut window_result = zero;
        for el in reusable_buckets.iter_mut().rev() {
            tmp += &*el;
            window_result += &tmp;
            if last_window == false {
                *el = zero;
            }
        }
        window_sums[window_idx] = window_result;

        window_start += c;
    }
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs (L196-202)
```rust
        );
        let cost_ergs = Ergs(cost * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs (L264-270)
```rust
        );
        let cost_ergs = Ergs(cost * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/pairing.rs (L36-40)
```rust
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/addition.rs (L22-26)
```rust
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/mappings.rs (L26-29)
```rust
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
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
