### Title
Unbounded SQL Correlated Subquery in `get_aggregated_balance_for_principal_at_block_idx` Enables Single-Request DoS of ICRC1 Rosetta Instance — (`rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs`)

---

### Summary

An unauthenticated HTTP client can send a single `POST /account/balance` request with `metadata.aggregate_all_subaccounts=true` targeting a principal with many subaccounts. This triggers an unbounded, correlated SQL subquery with no `LIMIT`, no timeout, and no pagination. Because `tokio_rusqlite` serializes all SQLite access through a single connection, the long-running query blocks every subsequent request to the Rosetta instance for its entire duration.

---

### Finding Description

**Entry point** — `account_balance_with_metadata` in `rs/rosetta-api/icrc1/src/data_api/services.rs`:

The function reads `aggregate_all_subaccounts` directly from user-supplied JSON metadata with no rate-limiting or authorization check: [1](#0-0) 

The only guard is a check that rejects requests where a *non-default* subaccount is also specified: [2](#0-1) 

When the subaccount is absent or is the default `[0u8; 32]`, execution falls through to: [3](#0-2) 

**Unbounded SQL query** — `get_aggregated_balance_for_principal_at_block_idx` in `rs/rosetta-api/icrc1/src/common/storage/storage_operations/mod.rs`: [4](#0-3) 

The outer `SELECT` returns every row in `account_balances` for the given principal. For each such row, the correlated inner `SELECT MAX(...)` re-scans all rows for that `(principal, subaccount)` pair up to `:block_idx`. There is no `LIMIT`, no `OFFSET`, no query timeout, and no cap on the number of subaccounts processed. Complexity is O(N × M) where N = distinct subaccounts and M = average balance-change events per subaccount.

**Blocking effect** — `StorageClient` wraps a single `tokio_rusqlite::Connection`: [5](#0-4) 

`tokio_rusqlite` serializes all calls through a single background thread. A long-running query therefore blocks every other storage operation (balance lookups, block fetches, transaction searches) for its entire duration.

---

### Impact Explanation

A single HTTP request can render the ICRC1 Rosetta instance completely unresponsive for the duration of the query. Exchanges and integrators that depend on Rosetta for balance queries, transaction lookups, and construction flows are all affected. Repeated requests keep the instance permanently degraded. The impact is scoped to the Rosetta sidecar; the IC ledger canister itself is unaffected.

---

### Likelihood Explanation

The endpoint requires no authentication. The precondition — a principal with a large number of distinct subaccounts — is realistic:

- DEXes, custodians, and exchanges routinely use per-user subaccounts under a single principal.
- ICRC1 tokens with low or zero fees make it cheap to manufacture millions of subaccounts.
- An attacker does not need to create the subaccounts themselves; they only need to identify an existing high-subaccount principal and query it repeatedly.

---

### Recommendation

1. **Add a hard cap** on the number of subaccounts aggregated per request (e.g., 10,000), returning an error if the principal exceeds it.
2. **Rewrite the query** to use a `GROUP BY (principal, subaccount)` with `MAX(block_idx)` rather than a correlated subquery, and add `LIMIT :max_subaccounts`.
3. **Add a SQLite `busy_timeout` / statement timeout** so runaway queries are cancelled rather than blocking indefinitely.
4. **Rate-limit** the `aggregate_all_subaccounts` path per source IP or per principal.

---

### Proof of Concept

```
# 1. Populate the Rosetta SQLite DB with a principal P having 10^6 distinct subaccounts
#    (each with at least one account_balances row).

# 2. Send a single balance request:
curl -X POST http://rosetta-host:8080/account/balance \
  -H 'Content-Type: application/json' \
  -d '{
    "network_identifier": {"blockchain":"Internet Computer","network":"<ledger_id>"},
    "account_identifier": {"address":"<principal_P>"},
    "metadata": {"aggregate_all_subaccounts": true}
  }'

# 3. Observe: the request hangs for minutes; all concurrent Rosetta requests
#    (balance, block, search) also hang for the same duration.
# 4. Repeat to keep the instance permanently degraded.
``` [6](#0-5)

### Citations

**File:** rs/rosetta-api/icrc1/src/data_api/services.rs (L255-260)
```rust
    // Check if aggregate_all_subaccounts flag is set in metadata
    let aggregate_all_subaccounts = metadata
        .as_ref()
        .and_then(|m| m.get("aggregate_all_subaccounts"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
```

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

**File:** rs/rosetta-api/icrc1/src/common/storage/storage_client.rs (L88-94)
```rust
#[derive(Debug)]
pub struct StorageClient {
    storage_connection: Connection,
    token_info: Option<TokenInfo>,
    flush_cache_and_shrink_memory: bool,
    balance_sync_batch_size: u64,
}
```
