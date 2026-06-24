Now I have all the information needed. Let me trace the full attack path.

**Entrypoint**: `POST /account/balance` with `metadata.aggregate_all_subaccounts=true` and no subaccount (or default `[0;32]`).

**Guard check** at `services.rs:274`: Only rejects if a *non-default* subaccount is explicitly set. A request with no subaccount passes through unconditionally.

**The SQL query** in `get_aggregated_balance_for_principal_at_block_idx` (`storage_operations/mod.rs:901-911`) is a **correlated subquery** — for every row in `account_balances` matching the principal, it executes an inner `SELECT MAX(block_idx)` subquery per (principal, subaccount) pair. There is no `LIMIT`, no timeout, and no cap on the number of subaccounts iterated.

---

### Title
Unbounded SQLite Correlated Subquery via `aggregate_all_subaccounts` Causes Rosetta Instance DoS — (`rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs`)

### Summary
An unprivileged HTTP client can send a single `POST /account/balance` request with `metadata.aggregate_all_subaccounts=true` targeting a principal with a large number of subaccounts. This triggers an unbounded correlated SQL scan with no timeout or row limit, blocking the Rosetta instance's SQLite thread pool and causing availability degradation.

### Finding Description
The guard at `account_balance_with_metadata` only rejects requests where a **non-default** subaccount is explicitly provided alongside `aggregate_all_subaccounts=true`: [1](#0-0) 

When no subaccount is provided (the common case for aggregation), execution falls through to: [2](#0-1) 

Which calls the storage operation: [3](#0-2) 

The SQL query is a correlated subquery — for each `account_balances` row matching the principal, it fires a separate `SELECT MAX(block_idx)` inner query per subaccount: [4](#0-3) 

There is no `LIMIT`, no row count cap, no query timeout, and no rate limiting on the HTTP endpoint. The `tokio_rusqlite` connection serializes all SQLite calls through a single thread; a long-running query blocks all subsequent DB operations for the entire Rosetta instance. [5](#0-4) 

### Impact Explanation
A single HTTP request targeting a principal with O(10^5–10^6) subaccounts causes the Rosetta instance's SQLite worker thread to be occupied for an extended period (seconds to minutes), blocking all concurrent balance queries, block fetches, and transaction lookups. This constitutes a constrained availability issue for the Rosetta service.

### Likelihood Explanation
The `/account/balance` endpoint is publicly reachable with no authentication. Large ICRC-1 ledgers (e.g., ckBTC, ckETH) have principals (exchanges, custodians) that legitimately accumulate many subaccounts. An attacker does not need to create those subaccounts — they only need to identify such a principal and issue the request. Repeated requests amplify the effect.

### Recommendation
1. Add a configurable cap on the number of subaccounts returned/summed (e.g., `LIMIT 10000`) in `get_aggregated_balance_for_principal_at_block_idx`.
2. Rewrite the correlated subquery as a non-correlated form using a `GROUP BY` + `MAX` to reduce per-row inner scans.
3. Add a SQLite `busy_timeout` and a per-query row-count guard.
4. Consider rate-limiting the `aggregate_all_subaccounts` path at the HTTP handler level.

### Proof of Concept
1. On any ICRC-1 ledger tracked by the Rosetta instance, mint or transfer tokens to 10^6 distinct subaccounts of a single principal `P`.
2. Send: `POST /account/balance` with body `{"network_identifier":…, "account_identifier":{"address":"<P_text>"},"metadata":{"aggregate_all_subaccounts":true}}`.
3. Observe that the request does not return within a normal timeout window and that concurrent requests to the same Rosetta instance stall.

### Citations

**File:** rs/rosetta-api/icrc1/src/data_api/services.rs (L269-278)
```rust
        let has_non_default_subaccount = match account.subaccount {
            None => false,
            Some(subaccount) => subaccount != [0_u8; 32],
        };

        if has_non_default_subaccount {
            return Err(Error::request_processing_error(
                &"Cannot specify subaccount when aggregate_all_subaccounts is true".to_owned(),
            ));
        }
```

**File:** rs/rosetta-api/icrc1/src/data_api/services.rs (L281-287)
```rust
        storage_client
            .get_aggregated_balance_for_principal_at_block_idx(
                &account.owner.into(),
                rosetta_block.index,
            )
            .await
            .map_err(|e| Error::unable_to_find_account_balance(&e))?
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs (L895-941)
```rust
pub fn get_aggregated_balance_for_principal_at_block_idx(
    connection: &Connection,
    principal: &PrincipalId,
    block_idx: u64,
) -> anyhow::Result<Nat> {
    // Query to get the latest balance for each subaccount of the principal at or before the given block index
    let mut stmt = connection.prepare_cached(
        "SELECT a1.subaccount, a1.amount
         FROM account_balances a1
         WHERE a1.principal = :principal
         AND a1.block_idx = (
             SELECT MAX(a2.block_idx)
             FROM account_balances a2
             WHERE a2.principal = a1.principal
             AND a2.subaccount = a1.subaccount
             AND a2.block_idx <= :block_idx
         )",
    )?;

    let rows = stmt.query_map(
        named_params! {
            ":principal": principal.as_slice(),
            ":block_idx": block_idx
        },
        |row| {
            let amount_str: String = row.get(1)?;
            Nat::from_str(&amount_str).map_err(|_| {
                rusqlite::Error::InvalidColumnType(
                    1,
                    "amount".to_string(),
                    rusqlite::types::Type::Text,
                )
            })
        },
    )?;

    let mut total_balance = Nat(BigUint::zero());
    for balance_result in rows {
        let balance = balance_result?;
        total_balance = Nat(total_balance
            .0
            .checked_add(&balance.0)
            .with_context(|| "Overflow while aggregating balances")?);
    }

    Ok(total_balance)
}
```

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_client.rs (L489-505)
```rust
    pub async fn get_aggregated_balance_for_principal_at_block_idx(
        &self,
        principal: &ic_base_types::PrincipalId,
        block_idx: u64,
    ) -> anyhow::Result<Nat> {
        let principal = *principal;
        Ok(self
            .storage_connection
            .call::<_, _, StorageError>(move |conn| {
                Ok(
                    storage_operations::get_aggregated_balance_for_principal_at_block_idx(
                        conn, &principal, block_idx,
                    )?,
                )
            })
            .await?)
    }
```
