### Title
Self-Transfer Balance Accounting Bug in ICP Rosetta Balance Book - (File: rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/blocks.rs)

### Summary
The `update_balance_book_execution` function in the ICP Rosetta ledger synchronizer contains a balance accounting bug in its self-transfer special-case handling. When a self-transfer (`from == to`) is processed and the account has no prior balance entry in the local SQLite database, the transaction fee is not deducted from the stored balance. This directly mirrors the external report's pattern: special-case logic for a "same-party" transaction fails to apply the correct balance adjustment, resulting in an inflated balance in the Rosetta balance book that is then served to API consumers.

### Finding Description
In `update_balance_book_execution`, the `Operation::Transfer` branch detects self-transfers:

```rust
let self_transfer = from.to_hex() == to.to_hex();
```

For the `Some(balance)` case, the self-transfer is handled correctly — the amount is added then subtracted (net zero), and the fee is deducted:

```rust
Some(mut balance) => {
    balance.1 += amount.get_e8s();
    if self_transfer {
        balance.1 -= amount.get_e8s();
        balance.1 -= fee.get_e8s();
    }
    new_balances.push(balance);
}
```

However, the `None` branch — reached when the account has no prior balance entry in the DB — does not distinguish between self-transfers and normal transfers:

```rust
None => {
    new_balances.push((to.to_hex(), amount.get_e8s()));
}
```

For a normal transfer, this is correct: the `to` account receives `amount` tokens. For a self-transfer, the correct net effect is `old_balance - fee`. The `None` branch instead records `amount` as the new balance, omitting the fee deduction entirely. The `if !self_transfer` debit block is also skipped, so no debit occurs for the `from` side either. [1](#0-0) 

The `account/balance` Rosetta endpoint reads directly from this balance book: [2](#0-1) 

### Impact Explanation
The Rosetta API's `/account/balance` endpoint is the primary interface used by exchanges and financial integrations to query ICP account balances. An exchange relying on this endpoint would receive an inflated balance (`amount` instead of `old_balance - fee`) for the affected account. This could cause the exchange to credit a user with more tokens than they actually hold, enabling over-withdrawal and direct financial loss to the exchange or counterparty. The balance book is the sole source of truth for the Rosetta node's balance responses.

### Likelihood Explanation
The `None` branch for a self-transfer is triggered when `extract_latest_balance(to)` returns no row — i.e., the account has no balance entry in the Rosetta DB at or before the current block index. This occurs when a Rosetta node begins syncing from a non-zero block index (e.g., after a partial sync, database reset, or balance-book pruning via `prune_account_balances`) and the account's first appearance in the synced range is a self-transfer. Any unprivileged ICP user can submit a self-transfer on the ledger; the triggering condition depends on the Rosetta node's sync state, which is an operational scenario that arises in practice. [3](#0-2) 

### Recommendation
The `None` branch must handle self-transfers explicitly. Since a self-transfer requires a pre-existing balance, the absence of a prior DB entry for a self-transfer is an error condition and should be rejected:

```rust
None => {
    if self_transfer {
        return Err(BlockStoreError::Other(format!(
            "Self-transfer from account {} with no prior balance at block {}",
            to, hb.index
        )));
    }
    new_balances.push((to.to_hex(), amount.get_e8s()));
}
```

### Proof of Concept
1. Deploy a Rosetta node and allow it to sync to block N. Then prune the balance book (via `prune_account_balances`) so that account A's balance entry before block N is removed, or configure the node to start syncing from block N+1.
2. Account A has a real ICP balance established before block N (so no entry exists in the Rosetta DB after pruning).
3. Account A submits a self-transfer (`from=A`, `to=A`, `amount=X`, `fee=F`) at block N+1.
4. The Rosetta node syncs block N+1 and calls `update_balance_book_execution`.
5. `extract_latest_balance(to)` returns `None` (no prior entry for A in DB).
6. The `None` branch executes: `new_balances.push((A.to_hex(), X))` — fee `F` is never deducted.
7. The Rosetta API now reports A's balance as `X` instead of `old_balance - F`.
8. An exchange querying `/account/balance` for A receives the inflated value `X` and may credit or permit withdrawal of `X` tokens rather than the correct `old_balance - F`. [4](#0-3)

### Citations

**File:** rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/blocks.rs (L449-463)
```rust
                let account_balance_opt = extract_latest_balance(to)?;
                let self_transfer = from.to_hex() == to.to_hex();
                match account_balance_opt {
                    Some(mut balance) => {
                        balance.1 += amount.get_e8s();
                        if self_transfer {
                            balance.1 -= amount.get_e8s();
                            balance.1 -= fee.get_e8s();
                        }
                        new_balances.push(balance);
                    }
                    None => {
                        new_balances.push((to.to_hex(), amount.get_e8s()));
                    }
                }
```

**File:** rs/rosetta-api/icp/ledger_canister_blocks_synchronizer/src/blocks.rs (L540-577)
```rust
    pub fn prune_account_balances(
        con: &mut Connection,
        block_idx: &u64,
    ) -> Result<(), BlockStoreError> {
        let mut stmt = con
            .prepare_cached(
                "SELECT DISTINCT account FROM account_balances WHERE block_idx <= ?1 AND account IN (SELECT account FROM account_balances WHERE block_idx <= ?1 GROUP BY account HAVING COUNT(block_idx) > 1)",
            )
            .map_err(|e| BlockStoreError::Other(e.to_string()))?;
        let mut rows = stmt
            .query(params![block_idx])
            .map_err(|e| BlockStoreError::Other(e.to_string()))?;
        let get_last_involved_block_idx = |acc: &str| -> Result<u64, BlockStoreError> {
            let command = "SELECT block_idx FROM account_balances WHERE block_idx <= ?1 AND account = ?2 ORDER BY block_idx DESC LIMIT 1";
            let mut stmt = con
                .prepare_cached(command)
                .map_err(|e| BlockStoreError::Other(e.to_string()))
                .unwrap();
            let mut block_idx = stmt
                .query_map(params![block_idx, acc], |row| Ok(row.get(0).unwrap()))
                .map_err(|e| BlockStoreError::Other(e.to_string()))?;
            match block_idx.next() {
                Some(Ok(idx)) => Ok(idx),
                Some(Err(e)) => Err(BlockStoreError::Other(e.to_string())),
                None => Ok(0),
            }
        };
        while let Some(row) = rows.next().unwrap() {
            let account: String = row.get(0).unwrap();
            let last_block_idx = get_last_involved_block_idx(&account)?;
            con.execute(
                "DELETE FROM account_balances WHERE account = ?1 AND block_idx < ?2",
                params![account, last_block_idx],
            )
            .map_err(|e| BlockStoreError::Other(e.to_string()))?;
        }
        Ok(())
    }
```

**File:** rs/rosetta-api/icp/src/request_handler.rs (L188-196)
```rust
        let block = self.get_block(msg.block_identifier).await?;
        let blocks = self.ledger.read_blocks().await;
        let tokens = blocks.get_account_balance(&account_id, &block.block_identifier.index)?;
        let amount = tokens_to_amount(tokens, self.ledger.token_symbol())?;
        Ok(AccountBalanceResponse {
            block_identifier: block.block_identifier,
            balances: vec![amount],
            metadata: neuron_info.map(|ni| ni.into()),
        })
```
