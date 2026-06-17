### Title
Pubdata Cost Systematically Undercharged Due to Floor Division in `native_per_pubdata` Calculation - (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

---

### Summary

ZKsync OS computes the native-resource cost per pubdata byte using integer floor division (`wrapping_div`), while the analogous computation for gas-to-native conversion uses ceiling division (`div_ceil`). This asymmetry causes pubdata costs — the ZKsync equivalent of L1 data fees — to be systematically undercharged. When `pubdata_price < native_price`, the truncation produces `native_per_pubdata = 0`, making pubdata completely free for users while the operator still bears the actual L1 publication cost.

---

### Finding Description

In `validate_and_compute_fee_for_transaction`, the per-pubdata-byte native resource rate is computed as:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs:142
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
``` [1](#0-0) 

This is **floor division**. By contrast, the gas-to-native conversion uses ceiling division:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs:135
u256_try_to_u64(&gas_price.div_ceil(native_price))
``` [2](#0-1) 

The same floor-division pattern is replicated in the public API helper:

```rust
// api/src/helpers.rs:427
let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
``` [3](#0-2) 

`native_per_pubdata` is then used throughout the resource lifecycle:

1. **Intrinsic pubdata overhead** deducted from the native limit at transaction start:
   `intrinsic_pubdata_overhead = native_per_pubdata_byte * intrinsic_pubdata` [4](#0-3) 

2. **Execution pubdata charging** via `get_resources_to_charge_for_pubdata`:
   `native = current_pubdata_spent * native_per_pubdata` [5](#0-4) 

3. **`delta_gas` adjustment** in `compute_gas_refund` that converts native consumption back to gas charged to the user:
   `delta_gas = (native_used / native_per_gas) - gas_used` [6](#0-5) 

When `native_per_pubdata` is truncated downward, `native_used` is understated, `delta_gas` is understated, and the operator receives less than the actual pubdata cost.

The double-resource accounting model is documented to use `nativePerGas := gasPrice/nativePrice` and charge pubdata from native resources: [7](#0-6) 

---

### Impact Explanation

**Operator under-compensation for pubdata (L1 data publication) costs:**

- **Case 1 — `pubdata_price < native_price`**: `native_per_pubdata = 0`. All pubdata is completely free for the user. The operator bears the full L1 publication cost with zero reimbursement. A user can write to many storage slots (generating hundreds of pubdata bytes) and pay nothing for the L1 data cost.

- **Case 2 — `pubdata_price` not a multiple of `native_price`**: The truncated remainder `(pubdata_price mod native_price)` per pubdata byte is never charged. For a transaction generating `N` pubdata bytes, the operator is short by approximately `N * (pubdata_price mod native_price)` base-token units.

The operator controls `native_price` and `pubdata_price` but may not be aware that non-multiple combinations cause systematic undercharging. Any unprivileged user who knows about this truncation can exploit it by maximizing pubdata generation (storage writes) when `pubdata_price mod native_price` is large.

---

### Likelihood Explanation

- **High reachability**: Any L2 transaction that writes to storage generates pubdata. No special privileges are required.
- **Operator-controlled parameters**: The operator sets `pubdata_price` and `native_price` independently via the oracle. There is no enforcement that `pubdata_price` must be a multiple of `native_price`.
- **Asymmetric rounding**: The inconsistency (`div_ceil` for gas, `wrapping_div` for pubdata) is a latent bug that activates whenever the operator sets prices that are not exact multiples of each other — a common real-world scenario.
- **Worst case is zero charge**: When `pubdata_price < native_price` (e.g., during low L1 fee periods), pubdata is entirely free, making storage-spam attacks economically rational.

---

### Recommendation

Replace the floor division with ceiling division for `native_per_pubdata`, consistent with how `native_per_gas` is computed:

```rust
// In validation_impl.rs:
let native_per_pubdata = u256_try_to_u64(&pubdata_price.div_ceil(native_price))
    .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

Apply the same fix in `api/src/helpers.rs` and in `process_l1_transaction.rs` where `native_per_pubdata` is derived from `gas_per_pubdata * native_per_gas` (that path already uses ceiling division for `native_per_gas`, so the L1 path may be less affected, but should be audited for consistency).

---

### Proof of Concept

**Setup**: Operator sets `native_price = 1000`, `pubdata_price = 999` (a plausible scenario where L1 fees are slightly below the native unit cost).

**Result**:
```
native_per_pubdata = 999 / 1000 = 0  (floor division)
```

A transaction that writes to 10 storage slots generates ~320 bytes of pubdata. With `native_per_pubdata = 0`:
- `get_resources_to_charge_for_pubdata` returns `native = 0`
- `delta_gas = 0` (no native overage)
- Operator receives `gas_used * gas_price` with zero pubdata component
- Actual L1 publication cost: `320 * 999 = 319,680` base-token units — entirely unrecovered

The existing test `test_pubdata_native_calculation_overflow` confirms the pubdata charging path is active and sensitive to `native_per_pubdata` values, but no test covers the truncation-to-zero case. [8](#0-7)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L134-138)
```rust
        } else {
            u256_try_to_u64(&gas_price.div_ceil(native_price)).ok_or(TxError::Validation(
                InvalidTransaction::NativeResourcesAreTooExpensive,
            ))?
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L141-143)
```rust
    // We checked native_price != 0 above
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price))
        .ok_or(TxError::Validation(InvalidTransaction::PubdataPriceTooHigh))?;
```

**File:** api/src/helpers.rs (L426-427)
```rust
    // native_per_pubdata = pubdata_price / native_price
    let native_per_pubdata = u256_try_to_u64(&pubdata_price.wrapping_div(native_price)).ok_or(())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L351-359)
```rust
    // Charge intrinsic pubdata
    let intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata);
    let native_limit = match native_limit.checked_sub(intrinsic_pubdata_overhead) {
        Some(val) => val,
        None => P::handle_arithmetic_error(
            system,
            P::native_underflow_error("subtracting pubdata overhead"),
        )?,
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-432)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L69-79)
```rust
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
```

**File:** docs/double_resource_accounting.md (L37-50)
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

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.
```

**File:** tests/instances/transactions/src/lib.rs (L1994-2054)
```rust
#[test]
fn test_pubdata_native_calculation_overflow() {
    use alloy::consensus::TxEip1559;
    use rig::alloy::primitives::TxKind;

    let mut tester = TestingFramework::new();
    let wallet = tester.random_signer();

    let to = address!("1234567890123456789012345678901234567890");
    /*
       contract A {
           mapping(uint256 => uint256) s;

           fallback() external payable {
               for (uint256 i = 0; i < 20; i++) {
                   s[i] = 0xfffffffffffffffffffffffff;
               }
           }
       }
    */
    // Spam some pubdata
    let bytecode = hex::decode("60806040525f5f90505b6014811015603f576c0fffffffffffffffffffffffff5f5f8381526020019081526020015f208190555080806001019150506009565b00fea2646970667358221220d8f4977e359f09d23e2979156755d7e177d43f8a1882a5a178eb98dd8bcb237264736f6c634300081f0033").unwrap();
    // Create a transaction that will generate significant pubdata
    let tx = {
        let tx = TxEip1559 {
            chain_id: 37u64,
            nonce: 0,
            gas_limit: 10000000,
            max_fee_per_gas: 1000000000000000000,
            max_priority_fee_per_gas: 1000000000000000000,
            to: TxKind::Call(to),
            value: U256::from(1000),
            ..Default::default()
        };
        ZKsyncTxEnvelope::from_eth_tx(tx, wallet.clone())
    };

    // Set extremely high native_per_pubdata to trigger overflow in current_pubdata_spent.checked_mul(native_per_pubdata)
    let native_price = U256::from(1);
    let pubdata_price = U256::from(u64::MAX / 200); // Huge pubdata price to trigger overflow

    let block_context = BlockContext {
        native_price,
        pubdata_price,
        eip1559_basefee: U256::from(1),
        ..Default::default()
    };
    tester = tester
        .with_balance(
            wallet.address(),
            U256::from_str("100000000000000000000010000").unwrap(),
        )
        .with_evm_contract(to, &bytecode)
        .with_block_context(block_context);
    let result = tester.execute_block(vec![tx]);

    // Verify the specific error is OutOfNativeResources
    match &result.tx_results[0].as_ref().unwrap().execution_result {
        rig::zksync_os_interface::types::ExecutionResult::Success(_) => panic!("Should fail"),
        rig::zksync_os_interface::types::ExecutionResult::Revert(_) => {}
    }
```
