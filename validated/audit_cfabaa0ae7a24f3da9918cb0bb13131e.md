### Title
Placeholder `L1_TX_NATIVE_PRICE = 10` Causes Incorrect Native Resource Budget for L1→L2 Transactions, Enabling Prover DoS - (`File: basic_bootloader/src/bootloader/constants.rs`)

---

### Summary

`L1_TX_NATIVE_PRICE` is hardcoded to `10` with an explicit `TODO` comment acknowledging the value has not been properly calibrated. This constant is the denominator used to compute the native resource budget for every L1→L2 priority transaction. If the correct value is significantly higher (as operator-set `native_price` values in tests suggest), L1→L2 transactions receive a disproportionately large native resource allowance, allowing an attacker to force the prover to perform far more proving work than the fee covers.

---

### Finding Description

In `basic_bootloader/src/bootloader/constants.rs`, the constant used as the native price for all L1→L2 transactions is:

```rust
// Default native price for L1->L2 transactions.
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
``` [1](#0-0) 

This constant is consumed unconditionally in `prepare_and_check_resources` inside `process_l1_transaction.rs`:

```rust
let native_price = L1_TX_NATIVE_PRICE;
let native_per_gas = ...
    u256_try_to_u64(&gas_price.div_ceil(native_price))...
``` [2](#0-1) 

The resulting `native_per_gas` is then multiplied by `gas_limit` to produce the transaction's native resource budget:

```
native_limit = gas_limit * (gas_price / L1_TX_NATIVE_PRICE)
``` [3](#0-2) 

For L2 transactions, the operator supplies `native_price` via block context to reflect actual proving costs. The docs explicitly state: *"for L1→L2 transactions we use a code constant instead of one provided by operator."* [4](#0-3) 

The `resources_for_tester` feature (which bypasses native accounting for testing) is **not** active in the production binary — the `production` feature set does not include it. [5](#0-4) 

The `TESTER_NATIVE_PER_GAS = 25_000` constant is correctly gated behind `cfg!(feature = "resources_for_tester")` in L2 validation. [6](#0-5) 

However, `L1_TX_NATIVE_PRICE = 10` has **no feature gate** and is used in production for every L1→L2 priority transaction.

---

### Impact Explanation

The native resource models the off-chain cost of generating a ZK proof. If `L1_TX_NATIVE_PRICE = 10` is orders of magnitude lower than the correct value (operator-set values in tests range from `100` to `1000`), then:

- `native_per_gas = gas_price / 10` is 10–100× higher than it should be
- `native_limit = gas_limit * native_per_gas` is correspondingly inflated
- L1→L2 transactions are granted a native budget far exceeding what their fee covers
- An attacker can submit L1→L2 transactions containing computationally expensive operations (e.g., modexp, ecrecover, large calldata hashing) that consume the full inflated native budget
- The prover must prove all this computation, but the fee collected does not cover the actual proving cost
- This constitutes a **prover DoS**: the prover is forced to do unbounded work relative to fees collected, potentially stalling block finalization on L1 and locking user funds in the rollup

The block-level native limit (`MAX_NATIVE_COMPUTATIONAL`) still applies, but an attacker can fill entire blocks with such transactions, each consuming the maximum native budget at minimal fee cost.

---

### Likelihood Explanation

- The `TODO (EVM-1157)` comment is a direct admission that the value is a placeholder, not a calibrated production constant — exactly analogous to the VADER `secondsPerEra = 1` testing value left in production.
- L1→L2 transactions are submitted by any user who deposits ETH on L1 — no privileged access is required.
- The attack requires only submitting standard L1→L2 priority transactions with a high `gas_limit` and computationally expensive calldata/execution.
- No feature flag, governance action, or operator cooperation is needed to trigger the vulnerable code path.

---

### Recommendation

1. Determine and set a calibrated value for `L1_TX_NATIVE_PRICE` that reflects the actual cost of a single native (RISC-V proving) cycle, consistent with the operator-set `native_price` used for L2 transactions.
2. Add a compile-time assertion or integration test that verifies `L1_TX_NATIVE_PRICE` is within a reasonable range relative to expected operator `native_price` values.
3. Remove the `TODO (EVM-1157)` placeholder before production deployment.

---

### Proof of Concept

1. Operator sets `native_price = 1000` for L2 transactions (a plausible production value).
2. Attacker submits an L1→L2 priority transaction with `gas_price = 1000`, `gas_limit = 72_000_000`.
3. L2 tx native budget: `72_000_000 * (1000 / 1000) = 72_000_000` native units.
4. L1→L2 tx native budget: `72_000_000 * (1000 / 10) = 7_200_000_000` native units — **100× larger**.
5. The attacker's transaction body executes expensive precompile calls (ecrecover, keccak loops) consuming the full inflated native budget.
6. The prover must prove 100× more work than the fee covers.
7. Repeated across multiple blocks, this stalls proof generation and prevents L1 finalization, locking user funds. [1](#0-0) [2](#0-1)

### Citations

**File:** basic_bootloader/src/bootloader/constants.rs (L64-66)
```rust
// Default native price for L1->L2 transactions.
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L453-479)
```rust
    // For L1->L2 txs, we use a constant native price to avoid censorship.
    let native_price = L1_TX_NATIVE_PRICE;
    let native_per_gas = if is_priority_op {
        if gas_price.is_zero() {
            if Config::SIMULATION {
                u256_try_to_u64(&system.get_eip1559_basefee().div_ceil(native_price))
                    .unwrap_or_else(|| {
                        system_log!(
                            system,
                            "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                        u64::MAX
                    })
            } else {
                FREE_L1_TX_NATIVE_PER_GAS
            }
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).unwrap_or_else(|| {
                system_log!(
                    system,
                    "Native per gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
            })
        }
    } else {
        // Upgrade txs are paid by the protocol, so we use a fixed native per gas
        FREE_L1_TX_NATIVE_PER_GAS
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L490-496)
```rust
    let native_prepaid_from_gas = native_per_gas.checked_mul(gas_limit)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native prepaid from gas calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });
```

**File:** docs/double_resource_accounting.md (L34-34)
```markdown
- `nativePrice` be a constant set by the operator, reflecting the "cost of processing a single cycle". Note: for L1->L2 transactions we use a code constant instead of one provided by operator.
```

**File:** forward_system/Cargo.toml (L48-49)
```text
# Features used for production, by both the singleblock_batch and multiblock_batch binaries.
production = ["basic_bootloader/eip-7702", "system_hooks/p256_precompile"]
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L126-128)
```rust
        if cfg!(feature = "resources_for_tester") {
            crate::bootloader::constants::TESTER_NATIVE_PER_GAS
        } else if Config::SIMULATION && gas_price.is_zero() {
```
