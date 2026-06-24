### Title
Aggregated Principal Balance Understated Due to Subaccount Collision Overwriting Earlier Balances - (File: rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs)

### Summary
The ICRC-1 Rosetta indexer's `update_account_balances` function stores balances using `effective_subaccount()`, collapsing `Account { subaccount: None }` and `Account { subaccount: Some([0u8; 32]) }` to the same DB bytes. If the in-memory `HashMap` cache treats these as distinct keys (because `Hash` may be derived from the raw `subaccount` field while `PartialEq` uses `effective_subaccount()`), each is written as a separate row in `account_balances` under the same `subaccount = [0;32]` bytes. The aggregation query `get_aggregated_balance_for_principal_at_block_idx` then returns only the **latest** row per subaccount, silently discarding all earlier balances — overwriting instead of accumulating, exactly as in the ReseedSilo report.

### Finding Description
**Root cause — storage layer (`update_account_balances`):**

When flushing the in-memory cache to SQLite, every account is keyed by `effective_subaccount()`:

```rust
":subaccount": account.effective_subaccount().as_slice(),
``` [1](#0-0) 

`effective_subaccount()` returns `[0u8; 32]` for both `None` and `Some([0u8; 32])`:

```rust
pub fn effective_subaccount(&self) -> &Subaccount {
    self.subaccount.as_ref().unwrap_or(DEFAULT_SUBACCOUNT)
}
```

<cite repo="Jaredbentat/ic--014"

### Citations

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L506-512)
```rust
                    .prepare_cached("INSERT INTO account_balances (block_idx, principal, subaccount, amount) VALUES (:block_idx, :principal, :subaccount, :amount)")?
                    .execute(named_params! {
                        ":block_idx": block_idx,
                        ":principal": account.owner.as_slice(),
                        ":subaccount": account.effective_subaccount().as_slice(),
                        ":amount": new_balance.to_string(),
                    })?;
```
