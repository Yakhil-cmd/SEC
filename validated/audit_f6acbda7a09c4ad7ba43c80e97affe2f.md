### Title
EVM Refund (EIP-3529) Is Completely Nullified When Native Resource Consumption Is the Binding Constraint — (`File: basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

In `compute_gas_refund`, the EVM SSTORE refund (EIP-3529) is subtracted from `gas_used` **before** the native-resource adjustment (`delta_gas`) is computed. Because `delta_gas` is measured against the already-reduced `gas_used`, the refund is algebraically cancelled out. Any user whose transaction is native-bound (high pubdata) and who also earned SSTORE refunds pays the full native-equivalent gas cost with zero benefit from those refunds — a direct, repeatable fund loss.

---

### Finding Description

`compute_gas_refund` in `refund_calculation.rs` executes the following sequence:

```
Step 1.  gas_used  = gas_limit - (ergs_remaining / ERGS_PER_GAS)          // from EVM execution
Step 2.  evm_refund = min(refund_counter / ERGS_PER_GAS,  gas_used / 5)   // EIP-3529 cap
Step 3.  gas_used -= evm_refund                                            // apply refund
Step 4.  gas_used  = max(gas_used, minimal_gas_used)
Step 5.  native_used = full_native_limit - native_remaining
Step 6.  delta_gas = (native_used / native_per_gas) - gas_used             // ← uses post-refund gas_used
Step 7.  if delta_gas > 0: gas_used += delta_gas
```

Let `G` = gas used from ergs (Step 1), `R` = evm_refund (Step 2), `N` = `native_used / native_per_gas` (Step 5).

When native is the binding constraint (`N > G − R`):

```
delta_gas  = N − (G − R)  =  N − G + R
final gas_used = (G − R) + (N − G + R)  =  N
```

The `R` terms cancel exactly. **The final `gas_used` equals `N` regardless of the EVM refund.** The user is charged `N × gas_price` instead of the correct `(N − R_correct) × gas_price`, where `R_correct = min(refund_counter, N/5)`.

The root cause is that `delta_gas` is computed against the post-refund `gas_used` (Step 6) rather than against the raw EVM gas used. The EVM refund reduces `gas_used`, which inflates `delta_gas` by the same amount, which then restores `gas_used` to `N`. The refund is silently absorbed.

The `// TODO: return delta_gas to gas_used?` comment at line 80 acknowledges that the negative-delta case is also unhandled, but the positive-delta case is the one that causes the nullification.

---

### Impact Explanation

Every transaction that simultaneously:
1. earns SSTORE refunds (e.g. clearing storage slots — a common pattern in DEX settlement, order-book clearing, token burn, etc.), and
2. is native-bound (pubdata cost exceeds EVM gas cost — common for any storage-heavy L2 transaction)

will have its entire EVM refund silently confiscated. The excess `gas_used` flows to the operator as additional fee revenue.

Maximum loss per transaction = `min(refund_counter_gas, N/5) × gas_price`. For a transaction with `N = 500 000` gas equivalent and `gas_price = 100 gwei`, the maximum loss is `100 000 × 100 gwei = 0.01 ETH`. This is repeatable on every such transaction and is not bounded by any protocol-level cap.

The same `compute_gas_refund` function is called for both L2 ZK transactions (via `ZkTransactionFlowOnlyEOA::before_refund`) and L1→L2 priority transactions (via `process_l1_transaction`), so both transaction types are affected.

---

### Likelihood Explanation

The condition is easily and routinely triggered:

- **Native-bound transactions** are the norm on ZKsync OS whenever pubdata costs are non-trivial (any transaction writing multiple storage slots).
- **SSTORE refunds** are earned by any contract that clears storage (ERC-20 `approve(0)`, DEX order cancellation, token burn, etc.).

No special privileges, oracle manipulation, or governance access are required. Any unprivileged user submitting a standard EVM transaction that clears storage slots while also writing pubdata will silently lose their EIP-3529 refund.

---

### Recommendation

Compute the native adjustment **before** applying the EVM refund cap, so the cap is based on the true effective gas used:

```rust
// 1. Compute raw gas used from ergs
let gas_used_from_ergs = gas_limit
    .checked_sub(resources.ergs().0.div_floor(ERGS_PER_GAS))
    .ok_or(internal_error!("gas remaining > gas limit"))?;
resources.exhaust_ergs();

// 2. Compute native adjustment first
let native_gas_equivalent = native_used / native_per_gas;
let effective_gas_used = gas_used_from_ergs.max(native_gas_equivalent);
let effective_gas_used = effective_gas_used.max(minimal_gas_used);

// 3. Apply EIP-3529 cap against the true effective gas used
let evm_refund = {
    let full_refund_gas = system.io.get_refund_counter().ergs().0.div_floor(ERGS_PER_GAS);
    let max_refund = effective_gas_used / 5;
    core::cmp::min(full_refund_gas, max_refund)
};

let final_gas_used = effective_gas_used - evm_refund;
```

This ensures the EVM refund actually reduces the fee paid, consistent with EIP-3529 semantics.

---

### Proof of Concept

**Setup:**
- `gas_limit = 100 000`
- EVM execution consumes `50 000` gas → `gas_used_from_ergs = 50 000`
- SSTORE refund counter = `10 000` gas (e.g. two storage slot clears)
- `native_per_gas = 2`, `native_used = 120 000` → `N = 60 000`

**Current behavior (`compute_gas_refund` as written):**

```
evm_refund = min(10 000, 50 000/5) = min(10 000, 10 000) = 10 000
gas_used   = 50 000 − 10 000 = 40 000
delta_gas  = 60 000 − 40 000 = 20 000
final gas_used = 40 000 + 20 000 = 60 000
user pays  = 60 000 × gas_price
```

**Correct behavior:**

```
effective_gas_used = max(50 000, 60 000) = 60 000
evm_refund = min(10 000, 60 000/5) = min(10 000, 12 000) = 10 000
final gas_used = 60 000 − 10 000 = 50 000
user pays  = 50 000 × gas_price
```

**Loss = 10 000 × gas_price** — the entire EVM refund is confiscated by the operator.

The attacker-controlled entry path is a standard EVM transaction (no special role required) that clears storage slots and writes pubdata. The vulnerable step is `compute_gas_refund` in `basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs` lines 39–81, called from both `ZkTransactionFlowOnlyEOA::before_refund` and `process_l1_transaction`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L38-81)
```rust
    // Following EIP-3529, refunds are capped to 1/5 of the gas used
    let evm_refund = {
        let full_refund_ergs = system.io.get_refund_counter().ergs();
        let full_refund_gas = full_refund_ergs.0.div_floor(ERGS_PER_GAS);
        let max_refund = gas_used / 5;
        core::cmp::min(full_refund_gas, max_refund)
    };

    system_log!(system, "Gas refund from refund counters = {evm_refund}\n");

    gas_used -= evm_refund;

    system_log!(
        system,
        "Minimal gas used from validation = {minimal_gas_used}\n"
    );

    #[allow(unused_mut)]
    let mut gas_used = core::cmp::max(gas_used, minimal_gas_used);

    // Note: for zero gas price, we use "unlimited native"
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
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L427-439)
```rust
        let refund_info = compute_gas_refund(
            system,
            to_charge_for_pubdata,
            transaction.gas_limit(),
            min_gas_used,
            context.native_per_gas,
            &mut context.resources.main_resources,
        )?;
        debug_assert_eq!(context.gas_used, 0);
        context.gas_used = refund_info.gas_used;
        context.gas_refunded = refund_info.evm_refund;
        context.total_pubdata = total_pubdata_used;
        context.native_used = refund_info.native_used;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L261-279)
```rust
    #[allow(unused_variables)]
    let RefundInfo {
        gas_used,
        evm_refund,
        native_used,
    } = compute_gas_refund(
        system,
        to_charge_for_pubdata,
        gas_limit,
        minimal_gas_used,
        native_per_gas,
        &mut resources,
    )?;

    // Transfer fee from treasury to operator
    // We already checked that total_gas_refund <= gas_limit
    let pay_to_operator = U256::from(gas_used)
        .checked_mul(U256::from(gas_price))
        .ok_or(internal_error!("gu*gp"))?;
```

**File:** docs/double_resource_accounting.md (L47-51)
```markdown
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.

```
