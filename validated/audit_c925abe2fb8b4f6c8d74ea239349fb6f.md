### Title
Inconsistent Minimum Gas Floor Enforcement Between Ethereum STF and ZK Transaction Flows - (`basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs`)

### Summary

The Ethereum STF transaction flow enforces a minimum gas floor of `TX_INTRINSIC_GAS` (21,000) when EIP-7623 is disabled, but the ZK L2 and L1 transaction flows use `minimal_gas_used = 0` from `compute_calldata_tokens` under the same condition. This allows EVM SSTORE refunds to reduce `gas_used` below the intrinsic cost in ZK transaction flows, while the Ethereum STF correctly prevents this. The inconsistency is a direct analog to the original report: a normalization/floor step is applied in one code path but omitted in a parallel one.

### Finding Description

**Root cause — divergent `minimal_gas_used` values when EIP-7623 is disabled:**

`compute_calldata_tokens` (shared by both ZK L1 and ZK L2 paths) returns `0` as the floor when the `eip_7623` feature is off:

```rust
// zk/validation_impl.rs  (also used by process_l1_transaction.rs)
#[cfg(not(feature = "eip_7623"))]
{
    (num_tokens, 0)   // ← minimal_gas_used = 0
}
```

The Ethereum STF validation computes the same value inline but hard-codes `TX_INTRINSIC_GAS` instead:

```rust
// ethereum/validation_impl.rs
#[cfg(not(feature = "eip_7623"))]
{
    (num_tokens, TX_INTRINSIC_GAS)   // ← minimal_gas_used = 21_000
}
```

**How the floor is consumed:**

For ZK L2 transactions, `minimal_gas_used = 0` is stored as `minimal_ergs_to_charge = Ergs(0)` and later converted back to `min_gas_used = 0` inside `before_refund`:

```rust
// zk/mod.rs  before_refund
let min_gas_used = context.minimal_ergs_to_charge.0 / ERGS_PER_GAS;  // = 0
let refund_info = compute_gas_refund(system, ..., min_gas_used, ...)?;
```

For ZK L1 transactions, `minimal_gas_used = 0` flows directly into `compute_gas_refund`:

```rust
// process_l1_transaction.rs
let (calldata_tokens, minimal_gas_used) =
    compute_calldata_tokens(system, transaction.calldata());  // returns (tokens, 0)
...
let RefundInfo { gas_used, .. } = compute_gas_refund(
    system, ..., minimal_gas_used, ...  // = 0
)?;
```

For the Ethereum STF, `minimal_gas_to_charge = TX_INTRINSIC_GAS = 21_000` is passed:

```rust
// ethereum/mod.rs  before_refund
let min_gas_used = context.minimal_gas_to_charge;  // = 21_000
let refund_info = compute_gas_refund(system, ..., min_gas_used, ...)?;
```

**Inside `compute_gas_refund`, the floor is applied as:**

```rust
let mut gas_used = core::cmp::max(gas_used, minimal_gas_used);
```

With `minimal_gas_used = 0` for ZK paths, EVM SSTORE refunds can push `gas_used` below 21,000. With `minimal_gas_used = 21_000` for the Ethereum STF, the floor is always enforced.

**Concrete exploit path:**

A user submits a ZK L2 transaction that clears a warm storage slot:
- Intrinsic gas: 21,000
- Warm SSTORE clear (non-zero → zero): 2,900 gas
- Total `gas_used` before refunds: 23,900
- EVM refund generated: 4,800 (SSTORE clear refund)
- Refund cap (EIP-3529, 1/5 of gas used): 23,900 / 5 = 4,780
- Applied refund: min(4,800, 4,780) = 4,780
- Final `gas_used` for ZK path: 23,900 − 4,780 = **19,120** (below 21,000)
- Final `gas_used` for Ethereum STF: max(19,120, 21,000) = **21,000**

The operator receives fees for 19,120 gas instead of the minimum 21,000, a shortfall of 880 gas per transaction.

### Impact Explanation

The inconsistency allows any ZK transaction sender to systematically underpay the operator relative to the intrinsic processing cost. By crafting transactions with warm SSTORE clears, an attacker can reduce the effective fee below `TX_INTRINSIC_GAS`. While the per-transaction shortfall is small (hundreds of gas units), it is repeatable by any unprivileged user and represents a resource accounting discrepancy between the two transaction flows. The operator's coinbase balance is undercredited relative to the work performed, matching the "accumulation of dust / discrepancy in accounting" impact class of the original report.

### Likelihood Explanation

The preconditions are minimal: any EOA that has previously written to a storage slot can clear it in a subsequent transaction. No privileged access, oracle manipulation, or cryptographic break is required. The gas window (21,000–26,250 total gas used before refunds) is easily hit with a single warm SSTORE clear on top of a simple call.

### Recommendation

In `compute_calldata_tokens`, when EIP-7623 is disabled, return `TX_INTRINSIC_GAS` as the floor instead of `0`, matching the Ethereum STF behavior:

```rust
#[cfg(not(feature = "eip_7623"))]
{
    (num_tokens, TX_INTRINSIC_GAS)   // enforce the same 21,000-gas floor
}
```

This ensures `compute_gas_refund` always clamps `gas_used` to at least the intrinsic cost in all three transaction flows (ZK L1, ZK L2, Ethereum STF), eliminating the accounting inconsistency.

### Proof of Concept

**Affected files and lines:**

- `compute_calldata_tokens` returns `(tokens, 0)` when EIP-7623 is disabled: [1](#0-0) 

- Ethereum STF inline equivalent returns `TX_INTRINSIC_GAS` instead: [2](#0-1) 

- ZK L2 `before_refund` uses `minimal_ergs_to_charge` (derived from the `0` above) as the floor: [3](#0-2) 

- ZK L1 passes `minimal_gas_used = 0` directly to `compute_gas_refund`: [4](#0-3) 

- Ethereum STF passes `minimal_gas_to_charge = TX_INTRINSIC_GAS = 21_000`: [5](#0-4) 

- `compute_gas_refund` applies the floor via `max(gas_used, minimal_gas_used)`: [6](#0-5) 

- `TX_INTRINSIC_GAS` constant is 21,000: [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L538-541)
```rust
    #[cfg(not(feature = "eip_7623"))]
    {
        (num_tokens, 0)
    }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/validation_impl.rs (L159-163)
```rust
        #[cfg(not(feature = "eip_7623"))]
        {
            (num_tokens, TX_INTRINSIC_GAS)
        }
    };
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L426-434)
```rust
        let min_gas_used = context.minimal_ergs_to_charge.0 / ERGS_PER_GAS;
        let refund_info = compute_gas_refund(
            system,
            to_charge_for_pubdata,
            transaction.gas_limit(),
            min_gas_used,
            context.native_per_gas,
            &mut context.resources.main_resources,
        )?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L498-273)
```rust

```

**File:** basic_bootloader/src/bootloader/transaction_flow/ethereum/mod.rs (L474-485)
```rust
        let min_gas_used = context.minimal_gas_to_charge;
        // Compute gas used following the same logic as in normal execution

        let refund_info = compute_gas_refund(
            system,
            S::Resources::empty(),
            transaction.gas_limit(),
            min_gas_used,
            0u64,
            &mut context.resources.main_resources,
        )?;
        context.gas_used = refund_info.gas_used;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/refund_calculation.rs (L55-57)
```rust
    #[allow(unused_mut)]
    let mut gas_used = core::cmp::max(gas_used, minimal_gas_used);

```

**File:** basic_bootloader/src/bootloader/constants.rs (L42-42)
```rust
pub const TX_INTRINSIC_GAS: u64 = 21_000;
```
