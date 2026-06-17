### Title
Unvalidated `gas_per_pubdata_limit = 0` in L1→L2 Transactions Allows Free Pubdata Generation - (`basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

L1→L2 priority transactions carry a user-supplied `gas_per_pubdata_limit` field (a `u32`) that controls how much gas the sender is willing to pay per byte of pubdata. The ZKsync OS bootloader reads this value directly and uses it to compute `native_per_pubdata`, the per-byte cost charged against the transaction's native resource budget. No minimum threshold is enforced on this field in the L2 bootloader. When a sender sets `gas_per_pubdata_limit = 0`, the derived `native_per_pubdata` is also `0`, making every pubdata check trivially pass and allowing the transaction to generate an arbitrary amount of pubdata at zero cost to the sender, while the protocol must still publish that pubdata to L1.

---

### Finding Description

In `process_l1_transaction`, the `gas_per_pubdata_limit` field is read from the ABI-encoded transaction without any lower-bound validation:

```rust
let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
``` [1](#0-0) 

This value is forwarded directly to `prepare_and_check_resources`, where `native_per_pubdata` is computed as:

```rust
let native_per_pubdata = (gas_per_pubdata as u64)
    .checked_mul(native_per_gas)
    .unwrap_or_else(|| { ... u64::MAX });
``` [2](#0-1) 

When `gas_per_pubdata = 0`, `native_per_pubdata = 0`. This zero propagates into two critical places:

**1. Intrinsic pubdata overhead in `create_resources_for_tx`:**

```rust
let intrinsic_pubdata_overhead = native_per_pubdata.saturating_mul(intrinsic_pubdata);
// = 0 * intrinsic_pubdata = 0  → no native deducted for intrinsic pubdata
``` [3](#0-2) 

**2. Post-execution pubdata check in `get_resources_to_charge_for_pubdata`:**

```rust
let native = current_pubdata_spent
    .checked_mul(native_per_pubdata)   // = anything × 0 = 0
    .ok_or(out_of_native_resources!())?;
``` [4](#0-3) 

The result is that `check_enough_resources_for_pubdata` always returns `enough = true` regardless of how many storage slots the transaction writes, because the resource cost for pubdata is always zero. [5](#0-4) 

The `validate_structure` function in `AbiEncodedTransaction` does not enforce a minimum on `gas_per_pubdata_limit` for L1→L2 transactions — it only checks that the field is zero for *non*-L1 transaction types. [6](#0-5) 

The code comment acknowledges that validation is expected to be done on L1, but explicitly notes this is a fallback assumption:

> "Note that the 'validation errors' are practically unreachable, as gas_limit, gas_price and gas_per_pubdata are either checked or set by the L1 contracts." [7](#0-6) 

The L2 bootloader itself contains no enforcement of a minimum `gas_per_pubdata_limit`.

---

### Impact Explanation

An attacker submits an L1→L2 priority transaction with `gas_per_pubdata_limit = 0` and calldata targeting a contract that writes to many storage slots. Because `native_per_pubdata = 0`, the post-execution pubdata check always passes. The attacker pays only the L1 gas for submitting the priority transaction; the protocol must pay L1 data-availability costs to publish all generated pubdata. For transactions that produce large state diffs, the protocol's L1 publication cost can far exceed the attacker's L1 submission cost, resulting in a direct financial loss to the protocol/operators. This is a **resource accounting bug** with a **public funds-loss path**.

---

### Likelihood Explanation

L1→L2 priority transactions are submitted by any unprivileged user directly to the L1 priority queue. No special role or key is required. The attacker only needs to craft a transaction with `gas_per_pubdata_limit = 0` and a callee that writes to storage. The L2 bootloader will process it without rejection. Likelihood is **medium**: the attacker bears L1 submission gas costs, but these are recoverable if the pubdata generated is large enough.

---

### Recommendation

Enforce a minimum threshold for `gas_per_pubdata_limit` in the L2 bootloader's L1 transaction processing path, analogous to how `gas_limit` is bounded. A reasonable minimum (e.g., matching the current block's pubdata price expressed in gas units) should be checked at the start of `process_l1_transaction` or inside `prepare_and_check_resources`. If the value is below the minimum, the bootloader should saturate it to the minimum (consistent with the existing resilience pattern for L1 transactions) and emit a system log, rather than silently allowing free pubdata.

---

### Proof of Concept

```
1. Attacker deploys a contract on L2 that writes to N storage slots in its fallback.
2. Attacker submits an L1→L2 priority transaction:
   - gas_per_pubdata_limit = 0
   - gas_limit = sufficient for EVM execution
   - gas_price = any nonzero value
   - to = attacker's storage-writing contract
   - total_deposited = gas_price * gas_limit  (satisfies the deposit check)
3. In process_l1_transaction:
   - gas_per_pubdata = 0
   - native_per_pubdata = 0 * native_per_gas = 0
4. Transaction executes, writes N storage slots, generating N * 32 bytes of pubdata.
5. check_enough_resources_for_pubdata: cost = N * 32 * 0 = 0 → always passes.
6. Transaction succeeds. Attacker paid only L1 submission gas.
   Protocol must publish N * 32 bytes of pubdata to L1 at its own cost.
```

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L77-80)
```rust
    // For L1->L2 transactions we always use the pubdata price provided by the transaction.
    // This is needed to ensure DDoS protection. All the excess expenditure
    // will be refunded to the user.
    let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L422-433)
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
/// log if this situation ever happens.
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L713-715)
```rust
    let (enough, to_charge_for_pubdata, pubdata_used) =
        check_enough_resources_for_pubdata(system, native_per_pubdata, resources, None)?;
    let is_success = !reverted && enough;
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
