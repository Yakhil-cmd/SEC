### Title
Zero Native Resource Cost for All EIP-2537 BLS12-381 Precompiles Allows Proving-Resource Exhaustion Without Payment - (`basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/`)

---

### Summary

All seven EIP-2537 BLS12-381 precompiles in ZKsync OS charge `cost_native = 0` for the native (proving) resource, despite performing computationally intensive elliptic-curve operations. This is the direct analog of the Beanstalk precision bug: instead of a missing division by a scaling factor, the entire native cost term is absent. An unprivileged user can craft a transaction that fills its EVM gas budget with BLS12-381 precompile calls, consuming real RISC-V proving cycles while paying zero native resource cost, breaking the economic model of the double-resource accounting system.

---

### Finding Description

ZKsync OS implements a **double resource accounting** model: every operation charges both EVM gas (ergs) and a "native" resource that models the actual RISC-V proving cost. The native limit for a transaction is `nativeLimit = gasLimit × nativePerGas`, and if native resources run out the transaction reverts.

All seven BLS12-381 precompiles hard-code `cost_native = 0`:

**G1/G2 Addition** (`addition.rs` lines 22, 63):
```rust
let cost_ergs = Ergs(BLS12_381_G1_ADDITION_GAS * ERGS_PER_GAS);
let cost_native = 0;   // ← missing native cost
resources.charge(&R::from_ergs_and_native(cost_ergs, ...cost_native...))?;
```

**G1/G2 MSM** (`msm.rs` lines 198, 266):
```rust
let cost_ergs = Ergs(cost * ERGS_PER_GAS);
let cost_native = 0;   // ← missing native cost
```

**Pairing Check** (`pairing.rs` line 36):
```rust
let cost_native = 0;   // ← missing native cost
```

**Field-to-G1/G2 Mapping** (`mappings.rs` lines 26, 76):
```rust
let cost_native = 0;   // ← missing native cost
```

Compare this to every other computationally expensive precompile, which carries a substantial native cost:

| Precompile | Native cost |
|---|---|
| ECRECOVER | `native_with_delegations!(350_000, 43_000, 0)` ≈ 522 K |
| BN254 ECMUL | `native_with_delegations!(600_000, 41_000, 0)` ≈ 764 K |
| BN254 Pairing (base) | `native_with_delegations!(13_000_000, 500_000, 0)` ≈ 15 M |
| P256 Verify | `native_with_delegations!(500_000, 71_000, 0)` ≈ 784 K |
| Point Evaluation | `native_with_delegations!(49_900_000, 3_301_000, 0)` ≈ 63 M |
| **BLS12-381 (all)** | **0** |

BLS12-381 operations are computationally heavier than BN254 operations. The EVM gas schedule reflects this (BLS12-381 pairing: 37,700 + 32,600/pair vs BN254 pairing: 45,000 + 34,000/pair — comparable gas, but BLS12-381 requires more RISC-V cycles due to larger field arithmetic and bigint delegations).

**Attack path:**

1. Attacker submits a transaction with a large gas limit (e.g., 30 M gas).
2. The transaction body consists entirely of `CALL` instructions to the BLS12-381 precompile addresses (0x0b–0x11).
3. Each BLS12-381 pairing call costs ~70,300 EVM gas and 0 native resources.
4. With 30 M gas the attacker can execute ~427 pairing pairs.
5. The native resource counter never advances from these calls; the transaction completes successfully.
6. The prover must prove all 427 pairings, consuming millions of RISC-V cycles the user did not pay for.

---

### Impact Explanation

**Resource accounting bug / economic loss.** The native resource is the mechanism by which users pay for proving costs. Setting it to 0 for BLS12-381 precompiles means:

- The prover bears the full computational cost of BLS12-381 operations with no corresponding user payment.
- A single transaction can force the prover to do orders-of-magnitude more work than the user paid for, since the native limit is never consumed by these calls.
- At scale, this is a sustained economic drain on the protocol and a viable proving-layer DoS vector: an attacker repeatedly submits transactions that are cheap in gas but expensive to prove.

This matches the Immunefi scope category of **resource accounting bug** with direct financial impact on the protocol.

---

### Likelihood Explanation

- Requires no privileged access; any EOA can submit such a transaction.
- EIP-2537 precompiles are live in the codebase and registered in the hook storage via `initialize_eip_2537`.
- The attack is deterministic and repeatable with standard EVM tooling.
- The only cost to the attacker is EVM gas fees, which are far below the proving cost imposed on the protocol.

---

### Recommendation

Assign non-zero native costs to all BLS12-381 precompiles, consistent with the pattern used for BN254 and other expensive precompiles. The costs should be benchmarked against actual RISC-V cycle counts (including bigint delegations for field arithmetic). As a conservative starting point, BLS12-381 operations should be at least as expensive as their BN254 counterparts:

```rust
// addition.rs
- let cost_native = 0;
+ let cost_native = BLS12_381_G1_ADDITION_NATIVE_COST; // benchmark-derived

// pairing.rs
- let cost_native = 0;
+ let cost_native = BLS12_381_PAIRING_BASE_NATIVE_COST
+     + (num_pairs as u64) * BLS12_381_PAIRING_PER_PAIR_NATIVE_COST;

// msm.rs
- let cost_native = 0;
+ let cost_native = compute_native_cost(num_pairs); // proportional to pair count

// mappings.rs
- let cost_native = 0;
+ let cost_native = BLS12_381_MAP_TO_G1_NATIVE_COST; // benchmark-derived
```

---

### Proof of Concept

```
// Pseudocode: transaction body that exhausts proving budget for free
for i in 0..427:
    CALL(address=0x0f,  // BLS12_PAIRING_CHECK
         gas=70300,
         input=<valid_g1_g2_pair>)
// Result: 427 BLS12-381 pairings proven, 0 native resources charged,
// attacker pays only ~30M EVM gas worth of fees.
```

The native resource counter remains at its initial value throughout all 427 calls. The `compute_gas_refund` path in `refund_calculation.rs` will compute `native_used ≈ 0`, so `delta_gas = 0`, and the attacker receives a near-full gas refund on any unused gas — further reducing the effective cost. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/msm.rs (L197-202)
```rust
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

**File:** basic_bootloader/src/bootloader/block_flow/ethereum/hooks/eip_2537/mappings.rs (L25-30)
```rust
        let cost_ergs = Ergs(BLS12_381_FIELD_TO_G1_GAS * ERGS_PER_GAS);
        let cost_native = 0;
        resources.charge(&R::from_ergs_and_native(
            cost_ergs,
            <R::Native as zk_ee::system::Computational>::from_computational(cost_native),
        ))?;
```

**File:** basic_system/src/cost_constants.rs (L45-51)
```rust
pub const BN254_ECADD_NATIVE_COST: u64 = native_with_delegations!(46_000, 1650, 0);
pub const BN254_ECMUL_NATIVE_COST: u64 = native_with_delegations!(600_000, 41_000, 0);
pub const BN254_PAIRING_BASE_NATIVE_COST: u64 = native_with_delegations!(13_000_000, 500_000, 0);
pub const BN254_PAIRING_PER_PAIR_NATIVE_COST: u64 = BN254_PAIRING_BASE_NATIVE_COST;
pub const MODEXP_WORST_CASE_NATIVE_PER_GAS: u64 = 300;
pub const P256_NATIVE_COST: u64 = native_with_delegations!(500_000, 71_000, 0);
// TODO(EVM-1178) Add more vectors and benchmark cost better
```

**File:** docs/double_resource_accounting.md (L37-48)
```markdown
First we define the ratio between EVM gas and native resource as:
  `nativePerGas := gasPrice/nativePrice`
Note: for call simulation we use a constant for it, as gasPrice might be set to 0.

Next we define the limit for the native resource as:
  `nativeLimit := gasLimit * nativePerGas`

Then we process the transaction, charging both Ergs for EE execution and native resource for any kind of computation (EE, bootloader or system work).

If execution doesn't run out of native resources, we first charge for pubdata from native resource.
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L59-80)
```rust
    let full_native_limit = if cfg!(feature = "unlimited_native") || native_per_gas == 0 {
        u64::MAX - 1
    } else {
        gas_limit.saturating_mul(native_per_gas)
    };
    let native_used = full_native_limit.saturating_sub(resources.native().remaining().as_u64());

    #[cfg(not(feature = "unlimited_native"))]
    {
        // Adjust gas_used with difference with used native
        let delta_gas = if native_per_gas == 0 {
            0
        } else {
            (native_used / native_per_gas) as i64 - (gas_used as i64)
        };

        if delta_gas > 0 {
            // In this case, the native resource consumption is more than the
            // gas consumption accounted for. Consume extra gas.
            gas_used += delta_gas as u64;
        }
        // TODO: return delta_gas to gas_used?
```
