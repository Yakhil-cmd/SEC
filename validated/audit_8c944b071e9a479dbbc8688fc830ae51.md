### Title
Missing Fee Collector Credit for `Approve` Operations in Rosetta ICRC-1 Balance Sync — (`rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs`)

### Summary

The `sync_account_balances` function in the ICRC-1 Rosetta API applies an asymmetric fee-collector crediting strategy across operation types. `Burn`, `Mint`, and `Transfer` operations call `get_107_fee_collector_or_legacy()` to resolve the fee collector (handling both the ICRC-107 `FeeCollector` block mechanism and the legacy per-block `fee_collector` field). The `Approve` operation branch, however, only checks `current_fee_collector_107` directly, skipping the legacy resolution path entirely. As a result, every `Approve` transaction whose fee collector is specified via the legacy mechanism debits the sender's balance but never credits the fee collector, permanently understating the fee collector's balance in Rosetta's local SQLite database.

### Finding Description

In `sync_account_balances`, the `Burn`, `Mint`, and `Transfer` arms all resolve the fee collector through `get_107_fee_collector_or_legacy`:

```rust
// Burn / Mint / Transfer
if let Some(collector) = get_107_fee_collector_or_legacy(
    &rosetta_block, connection, current_fee_collector_107)? {
    credit(collector, fee, rosetta_block.index, connection, &mut account_balances_cache)?;
}
```

The `Approve` arm uses a direct pattern-match on `current_fee_collector_107` instead:

```rust
// Approve — missing get_107_fee_collector_or_legacy call
if let Some(Some(collector)) = current_fee_collector_107 {
    credit(collector, fee, rosetta_block.index, connection, &mut account_balances_cache)?;
}
```

`current_fee_collector_107` is only populated when a `FeeCollector` operation block is encountered in the stream. When the fee collector is embedded directly in the block (the legacy mechanism), `current_fee_collector_107` remains `None` and the `Approve` branch silently skips the credit. The fee is still debited from the `from` account, so the total of all tracked balances no longer sums to the true circulating supply.

The codebase already acknowledges an identical historical bug for `Transfer` operations and ships a dedicated `repair_fee_collector_balances` migration to retroactively fix those records. No equivalent repair exists for `Approve` operations. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

Every `Approve` transaction processed against a ledger that uses the legacy fee-collector mechanism causes a permanent, cumulative undercount of the fee collector's balance in Rosetta's local database. Any downstream consumer of Rosetta balance queries (wallets, dashboards, exchange integrations) will observe a fee-collector balance that is lower than the on-chain truth by exactly the sum of all `Approve` fees paid since the Rosetta node was first synced. The discrepancy is monotonically increasing and cannot self-correct without a manual repair migration analogous to `repair_fee_collector_balances`. [4](#0-3) 

### Likelihood Explanation

Any ICRC-1 ledger that (a) configures a fee collector via the legacy mechanism and (b) has users submitting `Approve` or `TransferFrom` (ICRC-2) transactions triggers this path. ICRC-2 approvals are a standard, unprivileged operation callable by any token holder. No special role or key is required. The Rosetta API is the canonical off-chain indexer used by exchanges and wallets, making the corrupted balance data directly reachable by external parties. [1](#0-0) 

### Recommendation

Replace the direct `current_fee_collector_107` check in the `Approve` arm with the same `get_107_fee_collector_or_legacy` call used by `Burn`, `Mint`, and `Transfer`:

```rust
// Approve
let fee = rosetta_block.get_fee_paid()?.unwrap_or(Nat(BigUint::zero()));
debit(from, fee.clone(), rosetta_block.index, connection, &mut account_balances_cache)?;

if let Some(collector) = get_107_fee_collector_or_legacy(
    &rosetta_block, connection, current_fee_collector_107)? {
    credit(collector, fee, rosetta_block.index, connection, &mut account_balances_cache)?;
}
```

Additionally, extend `repair_fee_collector_balances` to cover `Approve` operation blocks so that existing Rosetta databases can be retroactively corrected. [5](#0-4) 

### Proof of Concept

1. Deploy an ICRC-1/ICRC-2 ledger with a `fee_collector_account` set via the legacy init argument (not via a `FeeCollector` block).
2. Mint tokens to `account_A`.
3. Call `icrc2_approve` from `account_A` to grant an allowance to `account_B`. The ledger deducts the fee from `account_A` and credits `fee_collector_account` on-chain.
4. Query the Rosetta API for `fee_collector_account`'s balance. It will be `0` (or stale from before the approve), while `icrc1_balance_of` on the ledger canister returns the correct non-zero value.
5. Repeat step 3 N times; the Rosetta-reported balance of `fee_collector_account` remains stuck while the on-chain balance grows by `N × fee`.

The existing test at `rs/rosetta-api/icrc1/src/common/storage/storage_operations/tests.rs` lines 469–487 demonstrates the exact same pattern for `Transfer` operations and confirms the repair path. No equivalent test exists for `Approve`. [4](#0-3) [1](#0-0)

### Citations

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L345-357)
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

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_client.rs (L514-534)
```rust
    /// Repairs account balances for databases created before the fee collector block index fix.
    /// This function identifies Transfer operations that used fee_collector_block_index but didn't
    /// properly credit the fee collector, and adds the missing fee credits.
    ///
    /// This is safe to run multiple times - it will only add missing credits and won't duplicate them.
    ///
    /// # Returns
    ///
    /// Returns `Ok(())` if the repair was successful, or an error if the repair failed.
    pub async fn repair_fee_collector_balances(&self) -> anyhow::Result<()> {
        let balance_sync_batch_size = self.balance_sync_batch_size;
        Ok(self
            .storage_connection
            .call::<_, _, StorageError>(move |conn| {
                Ok(storage_operations::repair_fee_collector_balances(
                    conn,
                    balance_sync_batch_size,
                )?)
            })
            .await?)
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
