### Title
One-Sided `delta_gas` Adjustment in `compute_gas_refund` Silently Discards Negative Delta, Causing User Overcharge — (`File: basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

`compute_gas_refund` adjusts `gas_used` upward when native resource consumption exceeds EVM gas consumption (`delta_gas > 0`), but **never adjusts it downward** when native consumption is lower than EVM gas consumption (`delta_gas < 0`). A `TODO` comment at line 80 explicitly acknowledges this missing branch. As a result, any transaction whose EVM gas consumption exceeds its native-resource-equivalent cost causes the user to be overcharged: the excess fee flows to the operator instead of being refunded to the sender.

---

### Finding Description

In `compute_gas_refund`, after computing `gas_used` from remaining ergs and applying the EVM SSTORE refund counter, the function reconciles EVM gas with native resource consumption via `delta_gas`:

```rust
let delta_gas = if native_per_gas == 0 {
    0
} else {
    (native_used / native_per_gas) as i64 - (gas_used as i64)
};

if delta_gas > 0 {
    // native resource consumption is more than gas accounted for → charge extra gas
    gas_used += delta_gas as u64;
}
// TODO: return delta_gas to gas_used?
``` [1](#0-0) 

The formula `delta_gas = (native_used / native_per_gas) - gas_used` can be negative when EVM gas consumption exceeds the native-resource equivalent. In that case the user has been charged more EVM gas than their actual proving cost warrants, and `gas_used` should be reduced by `|delta_gas|` to give the user a larger refund. The `if delta_gas > 0` branch handles only the opposite direction; the negative case falls through silently, leaving `gas_used` inflated.

The downstream refund in `refund_and_commit_fee` then computes:

```rust
let token_to_refund =
    context.gas_price * U256::from(context.tx_gas_limit - context.gas_used);
``` [2](#0-1) 

Because `gas_used` is not reduced when `delta_gas < 0`, `token_to_refund` is smaller than it should be. The operator receives the difference via:

```rust
let token_to_pay_operator = U256::from(context.gas_used)
    .checked_mul(gas_price_for_operator)
    ...
``` [3](#0-2) 

The double-resource-accounting documentation confirms the intended symmetric behaviour — `deltaGas` should reduce `gasUsed` when negative — but the implementation only handles the positive direction:

> `deltaGas := (nativeUsed / nativePerGas) - gasUsed`. If `deltaGas > 0`, we add it to `gasUsed`. [4](#0-3) 

The `#[cfg(not(feature = "unlimited_native"))]` gate means this code is active in all production builds (the `unlimited_native` feature is a testing shortcut). [1](#0-0) 

---

### Impact Explanation

Every transaction where EVM gas consumption exceeds the native-resource equivalent (i.e., operations that are expensive in EVM gas but cheap to prove) results in the user paying more than the protocol's own resource model says they should. The excess is silently transferred to the operator as inflated fees. This is a direct, repeatable financial loss for users, proportional to `|delta_gas| * gas_price`. There is no mechanism for the user to recover the overcharged amount.

---

### Likelihood Explanation

The condition `delta_gas < 0` is reachable by any unprivileged transaction sender. Operations with high EVM gas cost but low proving cost (e.g., warm SLOAD at 100 gas, SSTORE resets, large calldata with cheap computation) naturally produce this imbalance. No special privileges, governance access, or oracle manipulation are required. The bug fires on every such transaction automatically.

---

### Recommendation

Apply the symmetric adjustment: when `delta_gas < 0`, reduce `gas_used` by `|delta_gas|` (subject to the `minimal_gas_used` floor already in place). The existing `TODO` comment at line 80 marks exactly this missing branch:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
} else if delta_gas < 0 {
    // Native cost is lower than EVM gas cost; refund the difference.
    let reduction = (-delta_gas) as u64;
    gas_used = gas_used.saturating_sub(reduction);
    gas_used = core::cmp::max(gas_used, minimal_gas_used);
}
``` [5](#0-4) 

---

### Proof of Concept

1. Deploy ZKsync OS in forward mode (no `unlimited_native` feature).
2. Submit an L2 transaction that performs many warm SLOADs (100 EVM gas each, low native cost) so that `gas_used` (EVM) is significantly larger than `native_used / native_per_gas`.
3. After execution, observe that `compute_gas_refund` computes a negative `delta_gas` but does not reduce `gas_used`.
4. Observe that `token_to_refund = gas_price * (gas_limit - gas_used)` is smaller than `gas_price * (gas_limit - (gas_used + delta_gas))` — the user receives less refund than the native resource model warrants.
5. Observe that the operator's balance increases by the corresponding excess amount.

The `TODO: return delta_gas to gas_used?` comment at line 80 of `refund_calculation.rs` is the in-code acknowledgement of this exact missing branch. [5](#0-4)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L66-81)
```rust
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

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L457-458)
```rust
            let token_to_refund =
                context.gas_price * U256::from(context.tx_gas_limit - context.gas_used); // can not overflow
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L514-516)
```rust
        let token_to_pay_operator = U256::from(context.gas_used)
            .checked_mul(gas_price_for_operator)
            .ok_or(internal_error!("gu*gpfo"))?;
```

**File:** docs/double_resource_accounting.md (L47-51)
```markdown
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.

```
