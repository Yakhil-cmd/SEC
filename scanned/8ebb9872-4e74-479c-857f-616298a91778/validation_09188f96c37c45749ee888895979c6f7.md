### Title
Approve Operations Bypass Legacy Fee-Collector Credit in ICRC-1 Rosetta `account_balances` Table, Causing Inaccurate Balance Reporting - (File: `rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs`)

---

### Summary

The ICRC-1 Rosetta API maintains two separate data structures for transaction data: the `blocks` table (source of truth) and the derived `account_balances` table. The `update_account_balances` function processes blocks to populate `account_balances`, but it handles `Approve` and `Transfer` operations inconsistently with respect to legacy fee collectors. `Transfer` operations credit both ICRC-107 and legacy fee collectors, while `Approve` operations only credit ICRC-107 fee collectors. This causes the `account_balances` table to diverge from the true ledger state whenever `icrc2_approve` is called on a ledger with a legacy fee collector configured, leading to inaccurate balance reporting from the Rosetta API's `/account/balance` endpoint.

---

### Finding Description

In `rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs`, the `update_account_balances` function processes each block type differently.

For `Transfer` operations, the fee is credited to whichever fee collector is active — ICRC-107 or legacy — via `get_107_fee_collector_or_legacy`: [1](#0-0) 

For `Approve` operations, only the ICRC-107 fee collector (`current_fee_collector_107`) is credited. The legacy fee collector (embedded in the block's `fee_collector` field) is silently ignored: [2](#0-1) 

When the cache is flushed to the database, balances are written using `account.effective_subaccount()`: [3](#0-2) 

The result is that after an `Approve` operation with a legacy fee collector:
- The `from` account is correctly debited in `account_balances`
- The legacy fee collector account is **not** credited in `account_balances`
- The `blocks` table still records the full transaction correctly

This is a direct dual-mapping flaw: two structures track the same state, but one code path (`Approve`) fails to update both consistently. The existence of `repair_fee_collector_balances` in the same file confirms the developers recognized this class of bug for historical data, but the current code still exhibits it for `Approve` operations. [4](#0-3) 

The `get_account_balance_at_block_idx` query reads only from `account_balances`, so any divergence from the true ledger state is directly surfaced to API callers.

---

### Impact Explanation

Any caller of the Rosetta `/account/balance` endpoint will receive an understated balance for the legacy fee collector account after `Approve` operations. Simultaneously, the total balance across all accounts tracked in `account_balances` will not sum to the correct total (tokens are debited from `from` but not credited anywhere), violating ledger conservation within the Rosetta index. Exchanges, wallets, or financial services relying on the Rosetta API for balance queries will receive incorrect data, potentially leading to incorrect crediting or debiting of user accounts.

---

### Likelihood Explanation

Any unprivileged user can call `icrc2_approve` on any ICRC-1 ledger that has a legacy fee collector configured. The Rosetta API processes all blocks from the ledger automatically. No special privileges are required. The trigger is a standard, publicly available ICRC-2 endpoint. The condition (legacy fee collector) is a real deployment configuration used in production ledgers.

---

### Recommendation

In the `Approve` arm of `update_account_balances`, replace the ICRC-107-only check with the same `get_107_fee_collector_or_legacy` helper used by the `Transfer` arm, so that legacy fee collectors are also credited when an `Approve` fee is paid:

```rust
// Approve arm — replace:
if let Some(Some(collector)) = current_fee_collector_107 {
    credit(collector, fee, ...)?;
}

// With:
if let Some(collector) = get_107_fee_collector_or_legacy(
    &rosetta_block, connection, current_fee_collector_107)? {
    credit(collector, fee, ...)?;
}
```

Apply the same fix to `Burn` and any other operation types that debit a fee but do not credit the legacy fee collector. After the fix, extend `repair_fee_collector_balances` to also repair historical `Approve` blocks.

---

### Proof of Concept

The developers themselves document the bug class in the test suite: [5](#0-4) 

The test at line 474 explicitly marks the missing fee-collector credit as "the bug." The repair function confirms this is a known production issue for the `Transfer` case; the `Approve` case is not repaired.

To reproduce for `Approve`:
1. Configure an ICRC-1 ledger with a legacy fee collector (set `fee_collector` in block metadata).
2. Submit an `icrc2_approve` transaction from any account.
3. Call `update_account_balances` to process the block.
4. Query `get_account_balance_at_block_idx` for the fee collector account — it will show no credit for the approval fee.
5. Query `get_account_balance_at_block_idx` for the `from` account — it will show the fee was debited.
6. The sum of all balances in `account_balances` will be less than the true total supply tracked in `blocks`.

### Citations

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L418-445)
```rust
                crate::common::storage::types::IcrcOperation::Approve {
                    from,
                    spender: _,
                    amount: _,
                    expected_allowance: _,
                    expires_at: _,
                    fee: _,
                } => {
                    let fee = rosetta_block
                        .get_fee_paid()?
                        .unwrap_or(Nat(BigUint::zero()));
                    debit(
                        from,
                        fee.clone(),
                        rosetta_block.index,
                        connection,
                        &mut account_balances_cache,
                    )?;

                    if let Some(Some(collector)) = current_fee_collector_107 {
                        credit(
                            collector,
                            fee,
                            rosetta_block.index,
                            connection,
                            &mut account_balances_cache,
                        )?;
                    }
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L477-489)
```rust
                    if let Some(collector) = get_107_fee_collector_or_legacy(
                        &rosetta_block,
                        connection,
                        current_fee_collector_107,
                    )? {
                        credit(
                            collector,
                            fee,
                            rosetta_block.index,
                            connection,
                            &mut account_balances_cache,
                        )?;
                    }
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L502-514)
```rust
        let insert_tx = connection.transaction()?;
        for (account, block_idx_new_balances) in account_balances_cache.drain() {
            for (block_idx, new_balance) in block_idx_new_balances {
                insert_tx
                    .prepare_cached("INSERT INTO account_balances (block_idx, principal, subaccount, amount) VALUES (:block_idx, :principal, :subaccount, :amount)")?
                    .execute(named_params! {
                        ":block_idx": block_idx,
                        ":principal": account.owner.as_slice(),
                        ":subaccount": account.effective_subaccount().as_slice(),
                        ":amount": new_balance.to_string(),
                    })?;
            }
        }
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L863-891)
```rust
pub fn get_account_balance_at_block_idx(
    connection: &Connection,
    account: &Account,
    block_idx: u64,
) -> anyhow::Result<Option<Nat>> {
    Ok(connection
        .prepare_cached(
            "SELECT amount \
             FROM account_balances \
             WHERE principal = :principal \
             AND subaccount = :subaccount \
             AND block_idx <= :block_idx \
             ORDER BY block_idx \
             DESC LIMIT 1",
        )?
        .query(named_params! {
            ":principal": account.owner.as_slice(),
            ":subaccount": account.effective_subaccount(),
            ":block_idx": block_idx
        })?
        .mapped(|row| row.get(0))
        .next()
        .transpose()
        .with_context(|| {
            format!("Unable to fetch balance of account {account} at index {block_idx}")
        })?
        .map(|x: String| Nat::from_str(&x))
        .transpose()?)
}
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/tests.rs (L469-487)
```rust
    // Broken balances for block 2 (fee collector not credited)
    connection.execute("INSERT INTO account_balances (block_idx, principal, subaccount, amount) VALUES (2, ?1, ?2, '999999698')",
        params![from_account.owner.as_slice(), from_account.effective_subaccount().as_slice()])?;
    connection.execute("INSERT INTO account_balances (block_idx, principal, subaccount, amount) VALUES (2, ?1, ?2, '300')",
        params![to_account.owner.as_slice(), to_account.effective_subaccount().as_slice()])?;
    // Missing fee collector balance update - this is the bug

    // Verify broken state
    let fee_balance_before =
        get_account_balance_at_block_idx(&connection, &fee_collector_account, 2)?;
    assert_eq!(fee_balance_before, Some(Nat::from(1_u64))); // Should be 2, but it's 1 (broken)

    // Test repair function
    repair_fee_collector_balances(&mut connection, BALANCE_SYNC_BATCH_SIZE_DEFAULT)?;

    // Verify fixed state
    let fee_balance_after =
        get_account_balance_at_block_idx(&connection, &fee_collector_account, 2)?;
    assert_eq!(fee_balance_after, Some(Nat::from(2_u64))); // Now correctly 2
```
