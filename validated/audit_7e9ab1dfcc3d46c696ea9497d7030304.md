### Title
Hardcoded `L1_TX_NATIVE_PRICE` Causes Incorrect Native Resource Allocation for L1→L2 Priority Transactions - (File: `basic_bootloader/src/bootloader/constants.rs`)

---

### Summary

The bootloader uses a hardcoded constant `L1_TX_NATIVE_PRICE = 10` to compute native (proving) resource allocations for L1→L2 priority transactions, instead of reading the dynamic `native_price` from block metadata as L2 transactions do. When the actual block-level native price diverges significantly from 10, L1→L2 transaction senders receive a disproportionate amount of native computational resources relative to what they paid for, constituting a resource accounting bug.

---

### Finding Description

In `basic_bootloader/src/bootloader/constants.rs`, the constant is defined:

```rust
// Default native price for L1->L2 transactions.
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
``` [1](#0-0) 

In `process_l1_transaction.rs`, the `prepare_and_check_resources` function explicitly uses this hardcoded constant instead of the dynamic block-level `native_price`:

```rust
// For L1->L2 txs, we use a constant native price to avoid censorship.
let native_price = L1_TX_NATIVE_PRICE;
let native_per_gas = if is_priority_op {
    ...
    u256_try_to_u64(&gas_price.div_ceil(native_price))
    ...
}
``` [2](#0-1) 

In contrast, L2 transactions in `validation_impl.rs` read the dynamic price from block metadata:

```rust
let native_price = system.get_native_price(); // dynamic from oracle
let native_per_gas = u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [3](#0-2) 

The `native_price` field is a proper block-level parameter carried in `BlockMetadataFromOracle` and exposed via `ZkSpecificPricingMetadata::native_price()`: [4](#0-3) [5](#0-4) 

The `native_per_gas` value directly controls how many native computational (proving) resources are pre-allocated for the transaction via `native_prepaid_from_gas = native_per_gas * gas_limit`. [6](#0-5) 

---

### Impact Explanation

**Resource accounting bug.** The formula `native_per_gas = ceil(gas_price / native_price)` converts the gas price paid by the user into native proving resources per gas unit. When `L1_TX_NATIVE_PRICE = 10` is used instead of the actual block-level `native_price`:

- If `actual_native_price >> 10` (e.g., `native_price = 1000`): L1→L2 priority transactions receive `ceil(gas_price / 10)` native resources per gas unit — **100× more** than the `ceil(gas_price / 1000)` that L2 transactions would receive for the same gas price. The L1→L2 sender paid for gas on L1 at the L1 rate, but consumes proving resources at a 100× discount relative to the current native price.

- If `actual_native_price << 10` (e.g., `native_price = 1`): L1→L2 transactions receive far fewer native resources than they should, causing legitimate priority transactions to run out of native resources and fail — a liveness issue for the priority queue.

The first scenario allows an unprivileged attacker to submit L1→L2 transactions that consume excessive prover cycles without paying the appropriate cost, potentially exhausting the prover's native resource budget for a block and degrading throughput or causing DoS of the proving pipeline.

---

### Likelihood Explanation

The `native_price` is a dynamic block-level parameter set by the sequencer/operator and can change over time as proving costs evolve. The TODO comment (`// TODO (EVM-1157): find a reasonable value for it.`) explicitly acknowledges the hardcoded value of 10 is a placeholder. Any deployment where the actual `native_price` is set to a value significantly different from 10 (which is likely in production as proving costs are calibrated) triggers this discrepancy. The attacker entry path requires only submitting a standard L1→L2 priority transaction — no privileged access needed.

---

### Recommendation

Replace the hardcoded `L1_TX_NATIVE_PRICE` with the dynamic block-level `native_price` from `system.get_native_price()` in `prepare_and_check_resources`, consistent with how L2 transactions compute `native_per_gas`. If censorship-resistance requires a floor or cap on the effective native price for L1→L2 transactions, apply a bounded clamp (e.g., `min(actual_native_price, MAX_L1_NATIVE_PRICE)`) rather than ignoring the actual price entirely.

---

### Proof of Concept

1. Operator sets `native_price = 1000` in block metadata (reflecting increased proving costs).
2. Attacker submits an L1→L2 priority transaction with `gas_price = 10_000` and `gas_limit = 1_000_000`.
3. **L2 tx path** would compute: `native_per_gas = ceil(10_000 / 1_000) = 10`, `native_prepaid = 10 * 1_000_000 = 10_000_000`.
4. **L1→L2 path** computes: `native_per_gas = ceil(10_000 / 10) = 1_000`, `native_prepaid = 1_000 * 1_000_000 = 1_000_000_000` — **100× more native resources** allocated.
5. The transaction executes consuming up to 1 billion native units, while an equivalent L2 transaction would only be allocated 10 million. The attacker paid the same gas cost on L1 but consumed 100× the proving resources, underpaying the prover by a factor of 100. [1](#0-0) [7](#0-6)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L107-138)
```rust
    let native_price = system.get_native_price();

    let gas_price = if transaction.is_service() {
        // Service transactions do not pay gas fees,
        // their gas price is allowed to be < block base fee.
        U256::ZERO
    } else {
        get_gas_price::<S, Config>(
            system,
            transaction.max_fee_per_gas(),
            transaction.max_priority_fee_per_gas(),
        )?
    };

    let native_per_gas = {
        if native_price.is_zero() {
            return Err(internal_error!("Native price cannot be 0").into());
        }

        if cfg!(feature = "resources_for_tester") {
            crate::bootloader::constants::TESTER_NATIVE_PER_GAS
        } else if Config::SIMULATION && gas_price.is_zero() {
            // For simulation, if gas price isn't set, we use base fee
            // for native calculation
            u256_try_to_u64(&system.get_eip1559_basefee().div_ceil(native_price)).ok_or(
                TxError::Validation(InvalidTransaction::NativeResourcesAreTooExpensive),
            )?
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
```

**File:** zk_ee/src/system/metadata/zk_metadata.rs (L123-125)
```rust
    pub pubdata_price: U256,
    pub native_price: U256,
    pub coinbase: B160,
```

**File:** zk_ee/src/system/metadata/basic_metadata.rs (L60-63)
```rust
pub trait ZkSpecificPricingMetadata {
    /// Price of an unit of native resources.
    fn native_price(&self) -> U256;

```
