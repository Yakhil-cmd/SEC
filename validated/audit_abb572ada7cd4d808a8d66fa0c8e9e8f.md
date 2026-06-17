### Title
L1→L2 Deposit Processing Halts Entire Block When `transfer_from_treasury` Fails Due to Recipient Balance Overflow - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

The `mint_base_token` function in `process_l1_transaction.rs` calls `transfer_from_treasury`, which returns a fatal `MintingBalanceOverflow` error if the recipient's balance would overflow `U256::MAX`. This error propagates upward via `?` and halts the entire block — not just the individual transaction. An attacker can pre-load a target address (e.g., the coinbase/operator address) with a balance near `U256::MAX` and then submit an L1→L2 deposit transaction targeting that address, causing every subsequent L1 deposit in the block to fail at the block level.

---

### Finding Description

In `process_l1_transaction`, after the main transaction body executes, the bootloader performs three post-execution `mint_base_token` calls using `FORMAL_INFINITE` resources:

1. **Operator fee** → `mint_base_token(..., &pay_to_operator, &coinbase, ...)` at line 290
2. **Refund** → `mint_base_token(..., &to_refund_recipient, &refund_recipient, ...)` at line 338
3. **Value mint** (inside execution frame) → `mint_base_token(..., &to_transfer, &from, ...)` at line 634

Each of these calls `transfer_from_treasury`, which performs two balance updates:

```rust
// subtract from treasury
update_account_nominal_token_balance(..., treasury_address, ..., true, ...)
    .map_err(|_| interface_error!(TreasuryTransferFailed))?;

// add to recipient
update_account_nominal_token_balance(..., to, ..., false, ...)
    .map_err(|_| interface_error!(MintingBalanceOverflow))?;
```

If the recipient's balance + amount overflows `U256`, the second call returns `BalanceError::Overflow`, which is mapped to `MintingBalanceOverflow` and propagated via `?`.

The callers of `mint_base_token` for the operator fee and refund use `.map_err(|e| ...)?` — they propagate all errors that are not `OutOfErgs` or `FatalRuntimeError` directly upward:

```rust
.map_err(|e| match e.root_cause() {
    RootCause::Runtime(RuntimeError::OutOfErgs(_)) => { ... }
    RootCause::Runtime(RuntimeError::FatalRuntimeError(_)) => { ... }
    _ => e,   // <-- MintingBalanceOverflow falls here, propagated as-is
})?;
```

This `?` causes `process_l1_transaction` to return `Err(...)`, which propagates to the block-level transaction loop. The block loop does **not** catch this error as a per-transaction failure — it halts block processing entirely.

The `notify_l2_asset_tracker` function has the same fatal-halt design by explicit intent:

> "Failure halts block processing — if the asset tracker reverts, the chain's token accounting would be inconsistent, so we treat it as fatal rather than silently continuing."

But `MintingBalanceOverflow` from `transfer_from_treasury` is equally fatal and equally unrecoverable.

---

### Impact Explanation

**Vulnerability class:** Resource accounting bug / state-transition halt

An attacker who can pre-position a recipient address (coinbase, refund recipient, or the `from` address of an L1 tx) with a balance near `U256::MAX` can cause any L1→L2 deposit that sends tokens to that address to trigger `MintingBalanceOverflow`. Since this error is treated as a block-level fatal error (not a per-transaction revert), the entire block fails to finalize. This is a **Denial of Service on L1→L2 deposit processing** — the chain cannot include any L1 transactions in a block until the overflow condition is resolved.

The coinbase address is particularly dangerous: if an attacker can accumulate enough tokens at the coinbase address (e.g., by routing many L1 deposits through it as the operator), a subsequent L1 deposit's fee payment will overflow and halt the block.

---

### Likelihood Explanation

- The coinbase address is set by the operator and is publicly known.
- An attacker can send many L1→L2 transactions with the coinbase as the `to` address, accumulating its balance toward `U256::MAX`.
- Once the coinbase balance is near `U256::MAX`, any L1 deposit that pays a non-zero fee to the coinbase will trigger the overflow.
- `U256::MAX` is astronomically large in ETH terms, making this practically infeasible for ETH-denominated base tokens. However, for chains using a custom base token with a very small denomination or high inflation, this becomes more realistic.
- The `refund_recipient` field is attacker-controlled (set in `transaction.reserved[1]`), so an attacker can set it to an address they have pre-loaded with `U256::MAX - 1` tokens, making the refund overflow deterministic.

---

### Recommendation

1. In `mint_base_token` / `transfer_from_treasury`, treat `MintingBalanceOverflow` as a per-transaction failure (revert the transaction) rather than a block-level fatal error. The treasury subtraction has already succeeded at that point, so a rollback frame is needed.
2. Alternatively, cap recipient balances or validate that `recipient_balance + amount <= U256::MAX` before performing the transfer, and handle overflow as a transaction-level revert.
3. For the operator fee path specifically, consider routing fees through a dedicated accumulator that cannot overflow (e.g., wrapping arithmetic or a separate accounting mechanism).

---

### Proof of Concept

**Setup:** Deploy a chain where the base token has a small denomination. Pre-load `refund_recipient_address` with `U256::MAX - 1` tokens.

**Attack transaction:** Submit an L1→L2 priority transaction with:
- `reserved[1]` (refund recipient) = `refund_recipient_address`
- `gas_limit` large enough that `to_refund_recipient > 0`
- `gas_price > 0`

**Execution path:**

1. `process_l1_transaction` is called.
2. Main tx body executes (may succeed or fail — doesn't matter).
3. `mint_base_token(..., &pay_to_operator, &coinbase, ...)` succeeds.
4. `to_refund_recipient > 0`, so `mint_base_token(..., &to_refund_recipient, &refund_recipient_address, ...)` is called.
5. Inside `transfer_from_treasury`: treasury balance is decremented successfully.
6. `update_account_nominal_token_balance(..., refund_recipient_address, ..., false, ...)` → `(U256::MAX - 1) + to_refund_recipient` overflows → returns `BalanceError::Overflow`.
7. Mapped to `MintingBalanceOverflow` → propagated via `?`.
8. `process_l1_transaction` returns `Err(MintingBalanceOverflow)`.
9. Block loop receives the error → block processing halts.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L301-309)
```rust
    .map_err(|e| match e.root_cause() {
        RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
            internal_error!("Out of ergs on infinite ergs").into()
        }
        RootCause::Runtime(RuntimeError::FatalRuntimeError(_)) => {
            internal_error!("Out of native on infinite").into()
        }
        _ => e,
    })?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L336-359)
```rust
    if to_refund_recipient > U256::ZERO {
        let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
        mint_base_token::<S, Config>(
            system,
            system_functions,
            memories.reborrow(),
            &to_refund_recipient,
            &refund_recipient,
            l1_chain_id,
            &mut inf_resources,
            tracer,
            validator,
        )
        .map_err(|e| -> BootloaderSubsystemError {
            match e.root_cause() {
                RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
                    internal_error!("Out of ergs on infinite ergs").into()
                }
                RootCause::Runtime(RuntimeError::FatalRuntimeError(_)) => {
                    internal_error!("Out of native on infinite").into()
                }
                _ => e,
            }
        })?;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L813-831)
```rust
    let _ = system
        .io
        .update_account_nominal_token_balance(
            zk_ee::execution_environment_type::ExecutionEnvironmentType::EVM,
            resources,
            to,
            nominal_token_value,
            false, // false = add to balance
            fee_payment_in_simulation,
        )
        .map_err(|e| -> BootloaderSubsystemError {
            match e {
                SubsystemError::LeafUsage(balance_error) => {
                    system_log!(system, "Error while minting: {balance_error:?}");
                    interface_error!(BootloaderInterfaceError::MintingBalanceOverflow)
                }
                _ => wrap_error!(e),
            }
        })?;
```

**File:** basic_bootloader/src/bootloader/errors.rs (L248-255)
```rust
zk_ee::define_subsystem!(Bootloader,
interface BootloaderInterfaceError {
    CantPayRefundOverflow,
    CantPayOperatorOverflow,
    MintingBalanceOverflow,
    TopLevelInsufficientBalance,
    TreasuryTransferFailed,
},
```
