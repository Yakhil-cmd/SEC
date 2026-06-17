### Title
User-Controlled `gas_per_pubdata_limit = 0` Bypasses Pubdata Fee Enforcement for L1→L2 Transactions - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

L1→L2 priority transactions read `gas_per_pubdata_limit` directly from the user-supplied transaction field with no minimum-value enforcement in the bootloader. Setting this field to `0` collapses the entire pubdata fee to zero, allowing the transaction to generate arbitrary pubdata (storage writes, etc.) without paying for it, while the operator must still publish that pubdata to L1.

---

### Finding Description

In `process_l1_transaction`, the pubdata price per native unit is derived entirely from the user-supplied field:

```rust
let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();   // user-controlled u32
``` [1](#0-0) 

This value is then used to compute `native_per_pubdata`:

```rust
let native_per_pubdata = (gas_per_pubdata as u64)
    .checked_mul(native_per_gas)
    .unwrap_or_else(|| { u64::MAX });
``` [2](#0-1) 

When `gas_per_pubdata = 0`, `native_per_pubdata = 0`. This propagates into `get_resources_to_charge_for_pubdata`:

```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)   // 0 * anything = 0
    .ok_or(out_of_native_resources!())?;
``` [3](#0-2) 

With `native_per_pubdata = 0`, `resources_for_pubdata` is always empty, so `check_enough_resources_for_pubdata` always returns `enough = true` regardless of how much pubdata the transaction generates. [4](#0-3) 

Additionally, in `compute_gas_refund`, the `delta_gas` adjustment that would inflate `gas_used` to account for native pubdata consumption is skipped when `native_per_gas` is effectively zero for pubdata:

```rust
let delta_gas = if native_per_gas == 0 {
    0
} else {
    (native_used / native_per_gas) as i64 - (gas_used as i64)
};
``` [5](#0-4) 

The only deposit check performed is:

```rust
require_internal!(
    total_deposited >= tx_internal_cost,   // gas_price * gas_limit only
    "Deposited amount too low",
    system
)?;
``` [6](#0-5) 

There is no check that `gas_per_pubdata_limit` meets any minimum. The `L1TxBuilder` test helper even defaults this field to `0`:

```rust
gas_per_pubdata_byte_limit: 0,
``` [7](#0-6) 

---

### Impact Explanation

**Impact: High.** An L1→L2 transaction with `gas_per_pubdata_limit = 0` can:

1. Execute arbitrary storage writes, generating real pubdata that the operator must publish to L1.
2. Pay zero pubdata fee — `gas_used` is not inflated for pubdata overhead, so the operator receives only `gas_used_base * gas_price` with no pubdata component.
3. The operator bears the full L1 publication cost for that pubdata with no compensation.

The operator/protocol loses all pubdata fee revenue for every such transaction. Because pubdata publication is the dominant cost in ZK rollup operation, this is a direct and material financial loss.

---

### Likelihood Explanation

**Likelihood: High.** The entry path is fully permissionless:

- Any user submitting an L1→L2 transaction can set `gas_per_pubdata_byte_limit = 0` in the `ZKsyncL1Tx` struct.
- The bootloader comment explicitly acknowledges the value comes from the user: *"For L1->L2 transactions we always use the pubdata price provided by the transaction."*
- The default value in the test builder is already `0`, confirming this is a reachable and accepted input.
- No signature, governance, or privileged role is required. [8](#0-7) 

---

### Recommendation

Enforce a minimum `gas_per_pubdata_limit` for L1→L2 transactions in the bootloader, mirroring the approach used for L2 transactions where `native_per_pubdata` is derived from the operator-set block-level `pubdata_price` rather than a user-supplied field. Concretely, in `prepare_and_check_resources` for L1 transactions, reject (or saturate to a protocol minimum) any transaction where `gas_per_pubdata_limit` is below a sensible floor, and add a `require_internal!` check analogous to the existing deposit check.

---

### Proof of Concept

1. Craft an L1→L2 transaction with `gas_per_pubdata_byte_limit = 0`, a non-zero `gas_price`, and a callee contract that performs many `SSTORE` operations (generating significant pubdata).
2. Set `to_mint = gas_price * gas_limit` (no pubdata budget included).
3. Submit the transaction. The bootloader accepts it because `total_deposited >= gas_price * gas_limit` passes.
4. The contract executes, generating N pubdata bytes. Because `native_per_pubdata = 0`, `check_enough_resources_for_pubdata` returns `enough = true` unconditionally.
5. `gas_used` is not adjusted upward for pubdata. The operator receives `gas_used * gas_price` with zero pubdata component.
6. The operator must still publish N pubdata bytes to L1, paying L1 gas, with no compensation from the transaction fee.

This is directly confirmed by the existing test `test_l1_tx_not_enough_native_for_pubdata_burns_all_gas`, which shows that `gas_per_pubdata_byte_limit = 1` causes pubdata to be charged while `gas_per_pubdata_byte_limit = 0` (the default) does not. [9](#0-8)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L430-434)
```rust
    let native = current_pubdata_spent
        .checked_mul(native_per_pubdata)
        .ok_or(out_of_native_resources!())?;
    let native = <S::Resources as zk_ee::system::Resources>::Native::from_computational(native);
    Ok((current_pubdata_spent, S::Resources::from_native(native)))
```

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L451-455)
```rust
    let (pubdata_used, resources_for_pubdata) =
        get_resources_to_charge_for_pubdata(system, native_per_pubdata, base_pubdata)?;
    system_log!(system, "Checking gas for pubdata, resources_for_pubdata: {resources_for_pubdata:?}, resources: {resources:?}\n");
    let enough = resources.has_enough(&resources_for_pubdata);
    Ok((enough, resources_for_pubdata, pubdata_used))
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

**File:** tests/rig/src/utils/mod.rs (L337-337)
```rust
            gas_per_pubdata_byte_limit: 0,
```

**File:** tests/instances/transactions/src/native_charging.rs (L249-259)
```rust
    let make_tx = |gas_per_pubdata_byte_limit| {
        let tx: ZKsyncTxEnvelope = L1TxBuilder::new()
            .from(from)
            .to(TO)
            .gas_price(1000)
            .gas_limit(gas_limit.into())
            .gas_per_pubdata_byte_limit(gas_per_pubdata_byte_limit)
            .build()
            .into();
        tx
    };
```
