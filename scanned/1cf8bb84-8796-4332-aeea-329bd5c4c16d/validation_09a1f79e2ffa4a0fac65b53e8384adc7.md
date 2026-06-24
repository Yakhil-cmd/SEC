### Title
Unchecked Integer Arithmetic in Balance Book Causes Silent Overflow/Underflow - (File: rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/blocks.rs)

### Summary
The `update_balance_book_execution()` function in the ICP Rosetta API's ledger synchronizer performs raw `+=` and `-=` arithmetic on `u64` token balances without overflow/underflow protection. This is the direct analog of the "unsafe casting" and "unsafe arithmetic" issues described in the BestDexLens report: an unsigned integer is manipulated without bounds checking, and the result is then stored via an unsafe `u64 as i64` bit-reinterpretation cast into SQLite.

### Finding Description

In `database_access::update_balance_book_execution()`, the `Mint` and `Transfer` operations perform unchecked arithmetic on `balance.1` (a `u64`):

```rust
// Mint path – line 412
balance.1 += amount.get_e8s();   // no overflow check

// Transfer path – lines 453-456
balance.1 += amount.get_e8s();   // no overflow check
if self_transfer {
    balance.1 -= amount.get_e8s();  // no underflow check
    balance.1 -= fee.get_e8s();     // no underflow check
}
```

Additionally, the `Transfer` path computes `payable` without overflow protection:

```rust
let payable = amount.get_e8s() + fee.get_e8s();  // line 468 – unchecked addition
```

After arithmetic, the resulting `u64` balance is stored via an unsafe bit-reinterpretation cast:

```rust
let tokens_i64 = tokens as i64;  // line 489 – wraps silently for values > i64::MAX
```

This is a two-layer problem:
1. **Unsafe arithmetic**: `balance.1 += amount.get_e8s()` wraps silently in Rust's debug mode (panics) but in release mode wraps to 0 or a small value, corrupting the stored balance.
2. **Unsafe cast**: `tokens as i64` silently reinterprets large `u64` values as negative `i64` values in SQLite. The read-back path (`tokens_i64 as u64`) relies on this bit-reinterpretation being consistent, but SQLite's `INTEGER` type performs numeric comparisons on the stored signed value, meaning `ORDER BY block_idx DESC` queries may return the wrong row when multiple balance entries exist for the same account and the most recent one is stored as a negative `i64`.

The `get_account_balance` query uses `ORDER BY block_idx DESC LIMIT 1`, which is correct for block ordering. However, the `prune_account_balances` function uses `DELETE ... WHERE block_idx < ?2`, which could interact incorrectly with the negative-stored values if the block index itself were ever affected.

Contrast this with the correct pattern used elsewhere in the same codebase — `ic_ledger_core::balances::Balances::debit/credit` use `checked_sub`/`checked_add` with explicit error handling, and the ICRC-1 Rosetta storage uses `Nat` (arbitrary precision) for balances.

### Impact Explanation

An attacker who can cause a Rosetta API node to synchronize a sequence of ICP ledger blocks where a single account's balance exceeds `u64::MAX` (impossible in the canonical ledger, but possible if the Rosetta node processes a crafted or replayed block sequence) or where `amount + fee` overflows `u64` would cause the Rosetta node to store a silently corrupted balance. Any downstream consumer of the Rosetta API (exchanges, wallets, block explorers) querying `get_account_balance` would receive an incorrect balance, potentially enabling double-spend representations or denial of service for balance queries.

More concretely: the `payable = amount.get_e8s() + fee.get_e8s()` addition at line 468 is unchecked. If a block on the ICP ledger contains `amount = u64::MAX - 1` and `fee = 2`, this addition wraps to `1` in release mode, causing the balance deduction check (`balance.1 >= payable`) to pass incorrectly, and the sender's balance to be decremented by only `1` instead of the correct amount. The Rosetta node's balance book diverges from the canonical ledger state.

### Likelihood Explanation

The ICP ledger enforces that `amount + fee <= sender_balance <= u64::MAX`, so in practice no valid ICP ledger block will trigger the overflow. However, the Rosetta synchronizer is designed to replay arbitrary blocks from the ledger canister. A malicious or buggy ledger canister on a test subnet, or a future protocol change that relaxes token supply constraints, could trigger this path. The risk is medium-low for mainnet ICP but high for any deployment of this synchronizer against a non-mainnet ledger.

### Recommendation

1. Replace all raw `+=` and `-=` on `balance.1` with checked arithmetic:
   ```rust
   balance.1 = balance.1.checked_add(amount.get_e8s())
       .ok_or_else(|| BlockStoreError::Other("balance overflow".into()))?;
   ```
2. Replace `let payable = amount.get_e8s() + fee.get_e8s()` with:
   ```rust
   let payable = amount.get_e8s().checked_add(fee.get_e8s())
       .ok_or_else(|| BlockStoreError::Other("payable overflow".into()))?;
   ```
3. Replace the `tokens as i64` / `tokens_i64 as u64` bit-reinterpretation pattern with a `TEXT`-based storage (as already done in the ICRC-1 Rosetta storage), or at minimum add an explicit range check before the cast.
4. Add unit tests covering balances near `u64::MAX` and `amount + fee` overflow scenarios.

### Proof of Concept

The vulnerable lines are:

**Unchecked mint addition:** [1](#0-0) 

**Unchecked transfer addition and self-transfer subtraction:** [2](#0-1) 

**Unchecked `payable` addition:** [3](#0-2) 

**Unsafe `u64 as i64` cast on store:** [4](#0-3) 

**Unsafe `i64 as u64` bit-reinterpretation on read:** [5](#0-4) 

For comparison, the correct pattern used in the canonical ledger core: [6](#0-5) 

And the correct pattern in the ICRC-1 Rosetta storage: [7](#0-6)

### Citations

**File:** rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/blocks.rs (L352-355)
```rust
                // Read as i64 and reinterpret as u64 to handle the full u64 range
                let tokens_i64: i64 = row.get(0)?;
                let tokens_u64 = tokens_i64 as u64;
                Ok(tokens_u64)
```

**File:** rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/blocks.rs (L411-413)
```rust
                    Some(mut balance) => {
                        balance.1 += amount.get_e8s();
                        new_balances.push(balance);
```

**File:** rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/blocks.rs (L452-457)
```rust
                    Some(mut balance) => {
                        balance.1 += amount.get_e8s();
                        if self_transfer {
                            balance.1 -= amount.get_e8s();
                            balance.1 -= fee.get_e8s();
                        }
```

**File:** rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/blocks.rs (L468-468)
```rust
                            let payable = amount.get_e8s() + fee.get_e8s();
```

**File:** rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/blocks.rs (L488-491)
```rust
            // Store u64 as i64 using bit-reinterpretation to handle the full u64 range
            let tokens_i64 = tokens as i64;
            stmt_insert
                .execute(params![hb.index, account, tokens_i64])
```

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L178-181)
```rust
            balance = balance
                .checked_sub(&amount)
                .expect("Underflow while subtracting the amount from the balance");
            Ok(balance)
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L243-250)
```rust
        let new_balance = if let Some(balance) =
            get_account_balance_with_cache(&account, index, connection, account_balances_cache)?
        {
            Nat(balance.0.checked_sub(&amount.0).with_context(|| {
                format!(
                    "Underflow while debiting account {account} for amount {amount} at index {index} (balance: {balance})"
                )
            })?)
```
