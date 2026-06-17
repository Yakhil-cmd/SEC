### Title
Hardcoded `L1_TX_NATIVE_PRICE` Decouples L1→L2 Transaction Native Resource Accounting from Actual Proving Costs, Enabling Protocol-Subsidized Computation - (`basic_bootloader/src/bootloader/constants.rs`)

---

### Summary

`L1_TX_NATIVE_PRICE` is hardcoded to `10` in the bootloader constants and is used as the sole divisor when computing the native resource budget for every L1→L2 priority transaction. L2 transactions use a dynamic, operator-set `native_price` from the block oracle. When the actual proving cost (operator's `native_price`) diverges from `10`, L1 transactions receive a native resource budget that is either far too large (protocol subsidizes proving) or far too small (valid L1 txs run out of native and revert). An unprivileged user submitting L1→L2 transactions can exploit the over-allocation case to consume proving resources far beyond what they paid for.

---

### Finding Description

In `basic_bootloader/src/bootloader/constants.rs`, `L1_TX_NATIVE_PRICE` is defined as a compile-time constant with an explicit developer TODO acknowledging the value is not finalized:

```rust
// Default native price for L1->L2 transactions.
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
``` [1](#0-0) 

This constant is consumed in `prepare_and_check_resources` inside `process_l1_transaction.rs`, where it replaces the oracle-provided `native_price` that L2 transactions use:

```rust
// For L1->L2 txs, we use a constant native price to avoid censorship.
let native_price = L1_TX_NATIVE_PRICE;
let native_per_gas = ...
    u256_try_to_u64(&gas_price.div_ceil(native_price))...
``` [2](#0-1) 

For L2 transactions, the equivalent path in `validation_impl.rs` reads the dynamic block-level price:

```rust
let native_price = system.get_native_price();
...
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [3](#0-2) 

The documentation itself confirms the asymmetry: *"for L1→L2 transactions we use a code constant instead of one provided by operator."* [4](#0-3) 

The native resource limit is then:

```
nativeLimit = gas_limit * (gas_price / L1_TX_NATIVE_PRICE)
``` [5](#0-4) 

If the operator's actual `native_price` is, say, `1000` (reflecting increased proving costs), an L1 transaction with `gas_price = 1000` receives `native_per_gas = 1000/10 = 100` instead of the correct `1000/1000 = 1`. The user's native budget is **100× larger** than what they paid for, and the protocol absorbs the excess proving cost.

---

### Impact Explanation

**Vulnerability class**: Resource accounting bug / public funds-loss path.

When `L1_TX_NATIVE_PRICE = 10` is lower than the operator's actual `native_price`:

1. Every L1→L2 transaction receives a native resource budget proportional to `gas_price / 10` instead of `gas_price / actual_native_price`.
2. The attacker submits L1 transactions with computationally expensive execution (large calldata, complex EVM logic, precompile calls), consuming native resources far beyond what they paid for.
3. The operator/protocol bears the excess ZK proving cost for every such transaction.
4. Because L1 transactions **cannot be invalidated** (doing so would halt the priority queue), the system must process them regardless. [6](#0-5) 

The inverse case (`L1_TX_NATIVE_PRICE` too high) causes valid L1 transactions to run out of native resources and revert, consuming the user's full gas limit — a liveness/DoS impact on the priority queue. [7](#0-6) 

---

### Likelihood Explanation

**High**. The developer TODO comment at the definition site explicitly states the value of `10` is a placeholder pending a proper calibration (EVM-1157). The operator's `native_price` is a live, adjustable parameter that reflects real proving hardware costs. Any deployment where the operator sets `native_price` significantly above `10` (which is the expected production scenario as proving costs are measured in thousands of cycles per gas) immediately creates the over-allocation condition. Any unprivileged user with access to the L1 bridge can trigger this path by submitting a priority transaction. [1](#0-0) 

---

### Recommendation

Replace the hardcoded `L1_TX_NATIVE_PRICE` with the oracle-provided `native_price` from the block context, the same source used for L2 transactions. If a static floor is required for censorship-resistance reasons (to prevent the operator from setting `native_price` so high that L1 txs always run out of native), apply a **cap** rather than a full replacement:

```rust
// Use operator native_price, but cap it at a maximum to prevent censorship
let native_price = system.get_native_price().min(MAX_L1_TX_NATIVE_PRICE);
```

This ensures L1 transactions are charged proportionally to actual proving costs while preserving the anti-censorship property. The `MAX_L1_TX_NATIVE_PRICE` cap should be set to a value that guarantees any valid L1 transaction with a reasonable gas price can always obtain sufficient native resources.

---

### Proof of Concept

1. Operator sets `native_price = 10_000` (reflecting real proving costs).
2. Attacker submits an L1→L2 priority transaction with:
   - `gas_price = 100_000`
   - `gas_limit = 1_000_000`
   - Calldata invoking a computationally expensive EVM path (e.g., repeated `KECCAK256` or `ECRECOVER` calls).
3. Bootloader computes: `native_per_gas = 100_000 / 10 = 10_000` (using hardcoded `L1_TX_NATIVE_PRICE`).
4. Correct value would be: `native_per_gas = 100_000 / 10_000 = 10`.
5. Attacker's `nativeLimit = 1_000_000 * 10_000 = 10^10` vs. correct `1_000_000 * 10 = 10^7`.
6. The attacker executes 1000× more native-resource-intensive computation than they paid for.
7. The operator must prove the block at full cost, absorbing the 1000× excess proving work. [8](#0-7) [1](#0-0)

### Citations

**File:** basic_bootloader/src/bootloader/constants.rs (L64-66)
```rust
// Default native price for L1->L2 transactions.
// TODO (EVM-1157): find a reasonable value for it.
pub const L1_TX_NATIVE_PRICE: U256 = U256::from_limbs([10, 0, 0, 0]);
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L422-432)
```rust
///
/// Compute and perform some checks on fee/resource parameters.
/// This function handles cases that for L2 transactions would be
/// validation errors, as "invalidating" an L1 transaction can halt
/// the chain (due to the priority queue).
/// Note that the "validation errors" are practically unreachable, as
/// gas_limit, gas_price and gas_per_pubdata are either checked or set
/// by the L1 contracts. We decide to handle these cases as a fallback in
/// case the L1 contracts aren't properly updated to reflect a change in
/// ZKsync OS.
/// The approach is to use saturating arithmetic and emit a system
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L453-496)
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

    let native_per_pubdata = (gas_per_pubdata as u64)
        .checked_mul(native_per_gas)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native per pubdata calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });

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

**File:** docs/double_resource_accounting.md (L34-34)
```markdown
- `nativePrice` be a constant set by the operator, reflecting the "cost of processing a single cycle". Note: for L1->L2 transactions we use a code constant instead of one provided by operator.
```

**File:** tests/instances/transactions/src/native_charging.rs (L57-72)
```rust
// Test with a low cycles/gas ratio, should fail
#[test]
fn test_l1_tx_low_ratio() {
    let wallet = testing_signer(0);
    // L1 Txs have a hard-coded native price of 10
    let native_price = 10;
    let gas_price = native_price * LOW_RATIO;
    let tx = L1TxBuilder::new()
        .from(wallet.address())
        .to(TO)
        .gas_price(gas_price.into())
        .gas_limit(70_000)
        .input(hex::decode(ERC_20_TRANSFER_CALLDATA).unwrap())
        .build()
        .into();
    run_tx(tx, gas_price, native_price, false, false)
```
