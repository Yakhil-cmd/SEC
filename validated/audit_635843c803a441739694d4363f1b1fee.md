### Title
Unchecked `u64` Arithmetic in ICP Index Balance Tracking Causes Silent Overflow - (File: `rs/ledger_suite/icp/index/src/main.rs`)

### Summary

The ICP index canister's `process_balance_changes` function computes the total debit for a `Transfer` operation using a plain, unchecked `+` on two `u64` values. In a Rust release build, `u64` addition wraps silently on overflow. If a block is ever processed where `amount + fee > u64::MAX`, the index will debit a wrapped (near-zero) amount from the sender's tracked balance instead of the correct total, permanently inflating the sender's balance in the index state.

### Finding Description

In `rs/ledger_suite/icp/index/src/main.rs`, the `process_balance_changes` function handles `Operation::Transfer` by computing the total amount to debit from the sender as a bare `u64` addition:

```rust
debit(block_index, from, amount.get_e8s() + fee.get_e8s());
```

Both `amount.get_e8s()` and `fee.get_e8s()` return `u64`. In Rust release builds, `u64 + u64` wraps on overflow rather than panicking. If `amount + fee > u64::MAX`, the result wraps to a small value, and `debit` subtracts that small value from the sender's tracked balance instead of the correct total.

The ICRC1 index-ng (`rs/ledger_suite/icrc1/index-ng/src/main.rs`) performs the identical operation with a safe `checked_add`, trapping on overflow:

```rust
amount.checked_add(&fee).unwrap_or_else(|| {
    ic_cdk::trap(format!("token amount overflow while indexing block {block_index}"))
})
```

The ICP index canister lacks this protection entirely. [1](#0-0) [2](#0-1) 

### Impact Explanation

If a `Transfer` block is processed where `amount.get_e8s() + fee.get_e8s()` overflows `u64::MAX`, the ICP index canister silently debits a wrapped (near-zero) value from the sender's balance. The sender's balance in the index is then permanently inflated by approximately `u64::MAX` e8s (~184 billion ICP). This corrupts the index's certified balance state for that account. Any wallet, exchange, or application relying on the ICP index for balance queries would observe a grossly incorrect balance for the affected account. The actual ICP ledger is unaffected, but the index diverges from ground truth in a way that cannot be self-corrected without a canister upgrade and full re-sync. [3](#0-2) 

### Likelihood Explanation

The ICP ledger's `Balances::transfer` uses `checked_add` to validate `amount + fee` before accepting any transfer, so the ledger itself will never produce a block where `amount + fee > u64::MAX`. [4](#0-3) 

However, the index reads blocks from archive canisters, not directly from the ledger. If an archive canister were ever compromised, or if a future ledger bug allowed such a block to be committed, the index would process it silently and corrupt its state. The likelihood is low under normal operation but non-zero given the index's trust in archive data.

### Recommendation

Replace the unchecked addition with `checked_add`, trapping on overflow, consistent with the ICRC1 index-ng implementation:

```rust
Operation::Transfer { from, to, amount, fee, .. } => {
    let total = amount.get_e8s().checked_add(fee.get_e8s()).unwrap_or_else(|| {
        ic_cdk::trap(format!(
            "Block {block_index} caused an overflow when computing amount + fee for account {from}"
        ))
    });
    debit(block_index, from, total);
    credit(block_index, to, amount.get_e8s())
}
``` [5](#0-4) 

### Proof of Concept

1. Craft or inject a `Transfer` block into an archive canister where `amount = u64::MAX - 9_999` and `fee = 10_000`, so `amount + fee = u64::MAX + 1`, which wraps to `0`.
2. The ICP index canister fetches this block during its sync loop and calls `process_balance_changes`.
3. `debit(block_index, from, 0)` is called — the sender's balance is not reduced at all.
4. `credit(block_index, to, u64::MAX - 9_999)` is called — the receiver's balance is credited correctly.
5. The index now shows the sender retaining their full pre-transfer balance, permanently diverging from the actual ledger state. [6](#0-5)

### Citations

**File:** rs/ledger_suite/icp/index/src/main.rs (L495-508)
```rust
        Operation::Transfer {
            from,
            to,
            amount,
            fee,
            ..
        } => {
            debit(block_index, from, amount.get_e8s() + fee.get_e8s());
            credit(block_index, to, amount.get_e8s())
        }
        Operation::Approve { from, fee, .. } => debit(block_index, from, fee.get_e8s()),
    };
    Ok(())
}
```

**File:** rs/ledger_suite/icp/index/src/main.rs (L510-519)
```rust
fn debit(block_index: BlockIndex, account_identifier: AccountIdentifier, amount: u64) {
    change_balance(account_identifier, |balance| {
        if balance < amount {
            ic_cdk::trap(format!(
                "Block {block_index} caused an overflow for account_identifier {account_identifier} when calculating balance {balance} + amount {amount}"
            ))
        }
        balance - amount
    });
}
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L1073-1081)
```rust
                debit(
                    block_index,
                    from,
                    amount.checked_add(&fee).unwrap_or_else(|| {
                        ic_cdk::trap(format!(
                            "token amount overflow while indexing block {block_index}"
                        ))
                    }),
                );
```

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L110-114)
```rust
        let debit_amount = amount.checked_add(&fee).ok_or_else(|| {
            // No account can hold more than Tokens::max_value().
            let balance = self.account_balance(from);
            BalanceError::InsufficientFunds { balance }
        })?;
```
