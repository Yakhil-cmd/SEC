### Title
Asymmetric `deltaGas` Accounting in `compute_gas_refund` Causes Systematic User Overcharge - (File: `basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs`)

---

### Summary

`compute_gas_refund` applies the `deltaGas` correction only in one direction: when native resource consumption exceeds EVM gas consumption (`delta_gas > 0`), `gas_used` is increased and the user is charged more. When native resource consumption is *below* EVM gas consumption (`delta_gas < 0`), the code does nothing — the user is not refunded the difference. A `// TODO: return delta_gas to gas_used?` comment at the exact branch point explicitly acknowledges this asymmetry is unresolved. The result is that any transaction whose EVM gas consumption exceeds its native (proving) resource consumption is systematically overcharged, with the surplus flowing to the operator/coinbase.

---

### Finding Description

In `compute_gas_refund` (the single function that determines final `gas_used` for every L1 and L2 transaction), the dual-resource reconciliation step reads:

```rust
// basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs  lines 66-81
#[cfg(not(feature = "unlimited_native"))]
{
    let delta_gas = if native_per_gas == 0 {
        0
    } else {
        (native_used / native_per_gas) as i64 - (gas_used as i64)
    };

    if delta_gas > 0 {
        // native consumption > gas consumption → charge extra gas
        gas_used += delta_gas as u64;
    }
    // TODO: return delta_gas to gas_used?   ← acknowledged gap
}
```

The design intent, documented in `docs/double_resource_accounting.md` lines 47–51, is:

> `deltaGas := (nativeUsed / nativePerGas) - gasUsed`
> If `deltaGas > 0`, we add it to `gasUsed` … We expect the base fee to be enough to cover most transactions without the need of additional gas.

The documentation describes only the positive branch. The negative branch — where the user consumed *more* EVM gas than native resources — is silently dropped. `gas_used` is left at the higher EVM-derived value, so the user's refund (`gas_limit - gas_used`) is smaller than it should be, and the operator's fee (`gas_used * gas_price`) is larger than it should be.

The `gas_used` value produced by `compute_gas_refund` flows directly into:

- **ZK L2 transactions** (`zk/mod.rs` lines 436, 452–458, 514–516): `context.gas_used` drives both the user refund and the coinbase payment.
- **L1→L2 transactions** (`zk/process_l1_transaction.rs` lines 277–279): `pay_to_operator = gas_used * gas_price`.
- **Ethereum-type transactions** (`ethereum/mod.rs` lines 477–485, 508–518): same refund/fee split.

The `unlimited_native` compile-time feature bypasses the entire block; in production builds (RISC-V target, `zksync_os` binary) this feature is not set, so the asymmetric branch is live.

---

### Impact Explanation

**Direct financial loss to transaction senders.** For every transaction where `nativeUsed / nativePerGas < gasUsed`:

```
overcharge = (gasUsed - nativeUsed/nativePerGas) × gasPrice  [tokens]
```

The overcharged amount is silently transferred to the operator/coinbase instead of being refunded to the sender. There is no cap on the magnitude; a transaction that is gas-heavy but proving-cheap (e.g., many cold SLOADs, large calldata processing, or complex EVM arithmetic that is cheap in RISC-V cycles) can produce a large negative `delta_gas`.

This is a **resource accounting bug** with a direct, measurable token loss for users and an equivalent unearned gain for the operator — matching the Immunefi "loss of user funds" impact class.

---

### Likelihood Explanation

The condition `nativeUsed / nativePerGas < gasUsed` is not rare. It arises whenever EVM gas consumption outpaces native (proving) resource consumption. Concrete triggers:

- Transactions with large calldata that is cheap to prove but expensive in EVM intrinsic gas.
- Transactions that hit many warm storage slots (cheap native, still costs EVM gas).
- Any transaction where the EVM gas schedule is conservative relative to the actual RISC-V proving cost.

The `native_per_gas` ratio is non-zero for all standard L2 transactions (it is derived from `gasPrice / nativePrice`; `nativePrice` is enforced non-zero at validation, `zk/validation_impl.rs` line 122–123). The `unlimited_native` feature is off in production. Therefore the vulnerable path is reachable by any ordinary unprivileged transaction sender.

---

### Recommendation

Apply the negative `delta_gas` correction symmetrically:

```rust
if delta_gas > 0 {
    gas_used += delta_gas as u64;
} else if delta_gas < 0 {
    // Native consumption was lower than gas consumption;
    // refund the difference so the user is not overcharged.
    gas_used = gas_used.saturating_sub((-delta_gas) as u64);
    // Respect the minimal_gas_used floor already applied above.
    gas_used = core::cmp::max(gas_used, minimal_gas_used);
}
```

Remove the `// TODO` comment once the fix is applied. Update `docs/double_resource_accounting.md` to document both branches.

---

### Proof of Concept

**Setup (L2 EIP-1559 transaction):**

| Parameter | Value |
|---|---|
| `gas_limit` | 100 000 |
| `gas_price` | 1 000 wei |
| `native_price` | 50 (operator-set) |
| `native_per_gas` | `1000 / 50 = 20` |
| EVM gas consumed (ergs → gas) | 80 000 |
| Native resources consumed | 800 000 units |

**Calculation:**

```
native_used / native_per_gas = 800_000 / 20 = 40_000
gas_used (from ergs)          = 80_000
delta_gas                     = 40_000 - 80_000 = -40_000   (negative)
```

**Current behavior (buggy):** `delta_gas < 0` → branch not taken → `gas_used` stays at `80_000`.

```
user_refund   = (100_000 - 80_000) × 1_000 = 20_000_000 wei
operator_fee  = 80_000 × 1_000             = 80_000_000 wei
```

**Correct behavior:** `gas_used` should be reduced to `40_000`.

```
user_refund   = (100_000 - 40_000) × 1_000 = 60_000_000 wei
operator_fee  = 40_000 × 1_000             = 40_000_000 wei
```

**Overcharge per transaction:** `40_000 × 1_000 = 40_000_000 wei` silently redirected from the sender to the operator.

The entry path is a standard unprivileged `eth_sendRawTransaction` call. No privileged role, oracle manipulation, or external dependency is required. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/mod.rs (L477-488)
```rust
        let refund_info = compute_gas_refund(
            system,
            S::Resources::empty(),
            transaction.gas_limit(),
            min_gas_used,
            0u64,
            &mut context.resources.main_resources,
        )?;
        context.gas_used = refund_info.gas_used;

        Ok(())
    }
```
