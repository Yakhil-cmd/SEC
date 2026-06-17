### Title
L1 Transaction with `gas_per_pubdata = 0` Bypasses Pubdata Cost Accounting — (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

An L1→L2 transaction sender can set `gas_per_pubdata_limit = 0`, causing `native_per_pubdata = 0` throughout the entire transaction lifecycle. This silently disables the pubdata cost check, allowing the transaction to generate arbitrary amounts of pubdata (storage writes, logs) without paying any native resource cost for it. The protocol absorbs the L1 data-availability publication cost with no compensation.

---

### Finding Description

In `process_l1_transaction.rs`, `gas_per_pubdata` is read directly from the transaction and used without any non-zero validation:

```rust
let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
// ...
let native_per_pubdata = (gas_per_pubdata as u64)
    .checked_mul(native_per_gas)
    .unwrap_or_else(|| { ... u64::MAX });
``` [1](#0-0) [2](#0-1) 

When `gas_per_pubdata = 0`, the product `0 * native_per_gas = 0`, so `native_per_pubdata = 0`.

The deposit sufficiency check only validates `total_deposited >= gas_price * gas_limit`, which does **not** include pubdata cost:

```rust
let tx_internal_cost = gas_price.checked_mul(U256::from(gas_limit))...;
require_internal!(total_deposited >= tx_internal_cost, ...)?;
``` [3](#0-2) 

With `native_per_pubdata = 0`, two downstream accounting functions are neutralized:

1. **`create_resources_for_tx`**: `intrinsic_pubdata_overhead = native_per_pubdata_byte.saturating_mul(intrinsic_pubdata) = 0`, so no native is deducted for intrinsic pubdata. [4](#0-3) 

2. **`check_enough_resources_for_pubdata`**: `native = current_pubdata_spent.checked_mul(0) = 0`, so `enough` is always `true` regardless of how much pubdata the transaction generates. [5](#0-4) 

The `validate_structure` function in `abi_encoded/mod.rs` only enforces that `gas_per_pubdata_limit == 0` for **non-L1** transaction types. For `L1_L2_TX_TYPE`, any value including zero is accepted without complaint:

```rust
// gas_per_pubdata_limit should be zero for non L1 transactions
match tx_type {
    Self::UPGRADE_TX_TYPE | Self::L1_L2_TX_TYPE => {}  // no check at all
    _ => { if self.gas_per_pubdata_limit.read() != 0 { return Err(()); } }
}
``` [6](#0-5) 

The code comment at line 77–79 explicitly states that `gas_per_pubdata` is the DDoS-protection mechanism for L1 transactions, yet no lower-bound is enforced: [7](#0-6) 

---

### Impact Explanation

**Vulnerability class**: Resource accounting bypass via zero-value input.

An attacker submits an L1→L2 transaction with `gas_per_pubdata = 0` and a contract call that writes to many storage slots. Each SSTORE consumes native resources (bounded by `gas_limit * native_per_gas`), but the resulting pubdata bytes cost zero native. The protocol must publish all generated pubdata to L1 (paying real ETH for calldata/blob space) without receiving any compensation from the transaction sender. Within the block-level pubdata cap, the attacker can maximize pubdata generation per unit of gas paid, effectively subsidizing their L1 data costs at the protocol's expense.

---

### Likelihood Explanation

Any L1→L2 transaction sender can set `gas_per_pubdata_limit = 0` in the transaction struct. No privileged role is required. The bootloader performs no validation on this field for L1 transactions. The attack requires only a valid L1 deposit covering `gas_price * gas_limit`.

---

### Recommendation

Add a non-zero check for `gas_per_pubdata_limit` in `process_l1_transaction` (or in `validate_structure` for `L1_L2_TX_TYPE`):

```rust
// In process_l1_transaction, after reading gas_per_pubdata:
if gas_per_pubdata == U256::ZERO {
    // Log and saturate, consistent with L1 resilience policy,
    // or enforce a protocol minimum.
    system_log!(system, "L1 tx gas_per_pubdata is zero, using minimum\n");
    gas_per_pubdata = MINIMUM_GAS_PER_PUBDATA;
}
```

Alternatively, enforce a minimum in `validate_structure` for `L1_L2_TX_TYPE` to reject such transactions at the encoding-validation stage.

---

### Proof of Concept

**Textual PoC**:

1. Attacker deploys a contract on L2 that writes to 100 storage slots.
2. Attacker submits an L1→L2 transaction with:
   - `gas_per_pubdata_limit = 0`
   - `gas_price = P > 0`, `gas_limit = G` (sufficient to cover computation)
   - `total_deposited = P * G` (passes the deposit check)
3. Bootloader computes `native_per_pubdata = 0 * (P / L1_TX_NATIVE_PRICE) = 0`.
4. `create_resources_for_tx` charges 0 native for intrinsic pubdata.
5. Transaction executes, writing 100 storage slots → ~3,200 bytes of pubdata.
6. `check_enough_resources_for_pubdata` computes `resources_for_pubdata = 3200 * 0 = 0` → `enough = true`.
7. Transaction succeeds. Protocol publishes 3,200 bytes to L1 at its own cost.
8. Attacker paid only for EVM computation, not for pubdata.

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L77-80)
```rust
    // For L1->L2 transactions we always use the pubdata price provided by the transaction.
    // This is needed to ensure DDoS protection. All the excess expenditure
    // will be refunded to the user.
    let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L128-137)
```rust
    let tx_internal_cost = gas_price
        .checked_mul(U256::from(gas_limit))
        .ok_or(internal_error!("gp*gl"))?;
    let value = transaction.value.read();
    let total_deposited = transaction.reserved[0].read();
    require_internal!(
        total_deposited >= tx_internal_cost,
        "Deposited amount too low",
        system
    )?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L481-488)
```rust
    let native_per_pubdata = (gas_per_pubdata as u64)
        .checked_mul(native_per_gas)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native per pubdata calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });
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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-434)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
    let native = <S::Resources as zk_ee::system::Resources>::Native::from_computational(native);
    Ok((current_pubdata_spent, S::Resources::from_native(native)))
```

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L241-249)
```rust
        // gas_per_pubdata_limit should be zero for non L1 transactions
        match tx_type {
            Self::UPGRADE_TX_TYPE | Self::L1_L2_TX_TYPE => {}
            _ => {
                if self.gas_per_pubdata_limit.read() != 0 {
                    return Err(());
                }
            }
        }
```
