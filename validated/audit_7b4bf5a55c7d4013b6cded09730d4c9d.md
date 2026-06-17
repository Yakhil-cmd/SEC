### Title
Negative `delta_gas` Silently Discarded in `compute_gas_refund`, Causing User Overpayment to Operator - (`basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

### Summary

`compute_gas_refund` applies the `delta_gas` adjustment asymmetrically: when `delta_gas > 0` (native resource cost exceeds EVM gas cost), `gas_used` is increased and the user pays more. When `delta_gas < 0` (EVM gas cost exceeds native resource cost), the negative delta is silently discarded. The user is not refunded the difference, and the operator is overpaid. An explicit `// TODO: return delta_gas to gas_used?` comment in the code acknowledges this unresolved case.

### Finding Description

In `compute_gas_refund`, after computing `native_used` and `gas_used`, the function calculates:

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
// TODO: return delta_gas to gas_used?
``` [1](#0-0) 

The design intent, documented in `docs/double_resource_accounting.md`, is:

> `deltaGas := (nativeUsed / nativePerGas) - gasUsed`
> If `deltaGas > 0`, we add it to `gasUsed` [2](#0-1) 

The documentation only describes the positive case. The negative case — where EVM gas consumption exceeds native resource consumption — is not handled. The `gas_used` returned from `compute_gas_refund` is then used directly to compute both the user's refund and the operator's payment:

For L2 ZK transactions in `refund_and_commit_fee`:
- User refund: `gas_price * (tx_gas_limit - gas_used)` [3](#0-2) 
- Operator payment: `gas_used * gas_price_for_operator` [4](#0-3) 

For L1 transactions in `process_l1_transaction`:
- Operator payment: `gas_used * gas_price` [5](#0-4) 

When `delta_gas < 0`, `gas_used` is inflated relative to what native resource consumption warrants. The user is refunded less than they should be, and the operator receives the excess.

### Impact Explanation

Any transaction where EVM gas consumption exceeds native resource consumption (i.e., computationally cheap but gas-heavy operations) will result in the user overpaying the operator by `|delta_gas| * gas_price`. The excess is permanently transferred to the operator's coinbase address and is not recoverable by the user. This is a direct, quantifiable loss of user funds on every such transaction.

### Likelihood Explanation

This is triggered by any ordinary L2 or L1 transaction where the EVM gas path is more expensive than the native (proving) path. This is a realistic and common scenario — for example, transactions with large calldata, many storage reads, or operations that are gas-heavy but proving-cheap. Any unprivileged transaction sender can trigger this condition simply by submitting a transaction.

### Recommendation

Apply the negative `delta_gas` symmetrically: when `delta_gas < 0`, subtract `|delta_gas|` from `gas_used` (subject to the `minimal_gas_used` floor), so the user receives a refund for the excess gas charged beyond what native resource consumption warrants. The existing TODO comment at line 80 already flags this gap.

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
} else {
    // delta_gas < 0: EVM gas > native cost, refund the difference
    let reduction = (-delta_gas) as u64;
    gas_used = gas_used.saturating_sub(reduction).max(minimal_gas_used);
}
```

### Proof of Concept

Consider a ZK L2 transaction with:
- `gas_limit = 100_000`
- `native_per_gas = 10`
- After execution: `gas_used = 80_000` (from ergs), `native_used = 500_000`
- `delta_gas = (500_000 / 10) - 80_000 = 50_000 - 80_000 = -30_000`

Current behavior: `delta_gas < 0` is ignored. `gas_used` stays at `80_000`. User is refunded `20_000 * gas_price`. Operator receives `80_000 * gas_price`.

Correct behavior: `gas_used` should be reduced to `50_000`. User should be refunded `50_000 * gas_price`. Operator should receive `50_000 * gas_price`.

The user overpays by `30_000 * gas_price` on every such transaction. [6](#0-5)

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

**File:** docs/double_resource_accounting.md (L47-51)
```markdown
Then we compute the difference between the implicit gas used derived from native resource consumption and the gas used by EEs from the ergs used. We call this value `deltaGas`.
  `deltaGas := (nativeUsed / nativePerGas) - gasUsed`

If `deltaGas > 0`, we add it to `gasUsed` and charge it from ergs. This ensures that gas estimation will include additional gas to cover for native resources using just base fee. We expect the base fee to be enough to cover most transactions without the need of additional gas.

```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L452-458)
```rust
        if context.tx_gas_limit > context.gas_used {
            system_log!(system, "Gas price for refund is {:?}\n", &context.gas_price);

            // refund
            let refund_recipient = transaction.from();
            let token_to_refund =
                context.gas_price * U256::from(context.tx_gas_limit - context.gas_used); // can not overflow
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L514-516)
```rust
        let token_to_pay_operator = U256::from(context.gas_used)
            .checked_mul(gas_price_for_operator)
            .ok_or(internal_error!("gu*gpfo"))?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L277-279)
```rust
    let pay_to_operator = U256::from(gas_used)
        .checked_mul(U256::from(gas_price))
        .ok_or(internal_error!("gu*gp"))?;
```
