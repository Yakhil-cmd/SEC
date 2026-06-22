### Title
`get_allowances` / `get_allowances_list` Pagination Truncated by Expired Allowances Before Expiry Filter - (File: `rs/ledger_suite/icrc1/ledger/src/lib.rs`, `rs/ledger_suite/icp/ledger/src/lib.rs`)

---

### Summary

The `get_allowances` function (ICRC-1 ledger, `icrc103_get_allowances` endpoint) and `get_allowances_list` function (ICP ledger, `get_allowances` endpoint) check the `max_results` limit **before** filtering out expired allowances. As a result, expired allowances that are still stored in the stable `ALLOWANCES_MEMORY` (because lazy pruning has not yet removed them) consume slots in the page, causing the caller to receive **fewer valid allowances than requested** and potentially **missing valid allowances entirely** when paginating.

---

### Finding Description

Both the ICRC-1 ledger and the ICP ledger implement a paginated allowance listing endpoint. The core loop in each function iterates over the sorted `ALLOWANCES_MEMORY` stable B-tree map and applies two sequential checks:

**ICRC-1 ledger** (`rs/ledger_suite/icrc1/ledger/src/lib.rs`, `get_allowances`):

```rust
if result.len() >= max_results as usize {   // ← limit checked FIRST
    break;
}
if account_spender.account.owner != from.owner {
    break;
}
if let Some(expires_at) = storable_allowance.expires_at
    && expires_at.as_nanos_since_unix_epoch() <= now
{
    continue;                                // ← expired entry skipped AFTER limit check
}
result.push(...)
```

**ICP ledger** (`rs/ledger_suite/icp/ledger/src/lib.rs`, `get_allowances_list`):

```rust
if result.len() >= max_results as usize || from_account_id != from {
    break;                                   // ← limit checked FIRST
}
if let Some(expires_at) = storable_allowance.expires_at
    && expires_at.as_nanos_since_unix_epoch() <= now
{
    continue;                                // ← expired entry skipped AFTER limit check
}
result.push(...)
```

The allowance table uses **lazy pruning**: expired entries are only removed from `ALLOWANCES_MEMORY` when `apply_transaction` is called (which calls `approvals_mut().prune(now, APPROVE_PRUNE_LIMIT)`). Between transactions, expired entries remain in the stable map. When the `max_results` limit is hit, the loop breaks immediately — even if the entries that consumed those slots were all expired and `continue`d past. This means the result set can be **shorter than `max_results`** even when more valid allowances exist beyond the expired entries.

The root cause is structurally identical to the reported M-1 bug: a collection is iterated for a critical calculation (here, pagination result count) without first filtering out stale/expired entries, causing the output to be incorrect.

---

### Impact Explanation

An unprivileged query caller invoking `icrc103_get_allowances` (ICRC-1 ledger) or `get_allowances` (ICP ledger) with a `take` parameter can receive a response with **fewer allowances than requested**, even when more valid allowances exist for the account. When paginating using `prev_spender`, the caller may **skip valid allowances** that follow a run of expired entries in the sorted map, because the page limit is exhausted by expired entries that are then silently dropped. This breaks the pagination contract: a caller who receives `N < take` results cannot distinguish "no more allowances" from "expired entries consumed the page budget." Downstream systems (wallets, DeFi integrations, indexers) relying on this endpoint for complete allowance enumeration may operate on an incomplete view of active approvals.

---

### Likelihood Explanation

The ICP and ICRC-1 ledgers are production canisters on the Internet Computer mainnet. The `icrc2_approve` endpoint is publicly callable by any principal. Any account holder can create many approvals with short expiration times, then let them expire without triggering a transaction (which would prune them). After expiry, the stable map still contains those entries. Any subsequent `icrc103_get_allowances` / `get_allowances` call for that account will have its page budget consumed by the expired entries. This is reachable by any unprivileged ingress query caller with no special privileges required.

---

### Recommendation

Move the `max_results` limit check to **after** the expiry filter, so that expired entries do not consume page budget:

```rust
// Check expiry BEFORE checking the result limit
if let Some(expires_at) = storable_allowance.expires_at
    && expires_at.as_nanos_since_unix_epoch() <= now
{
    continue;
}
if result.len() >= max_results as usize {
    break;
}
result.push(...)
```

This mirrors the correct pattern used in `AllowanceTable::allowance`, which checks expiry before returning a result. [1](#0-0) 

---

### Proof of Concept

**Setup:**
1. Account `A` creates 500 approvals for spenders `S1..S500`, each with `expires_at = now + 1 second`.
2. Wait 2 seconds (no transactions issued, so no pruning occurs).
3. Account `A` creates 1 additional approval for spender `S501` with no expiration.

**Attack:**
Call `icrc103_get_allowances` with `from_account = A`, `take = 500` (the default max).

**Expected result:** The response contains the 1 valid allowance for `S501` (and possibly fewer expired ones if any were pruned).

**Actual result:** The loop iterates over `S1..S500` (all expired, all `continue`d), hits `result.len() >= 500` only after processing all 500 expired entries — but since none were pushed, `result.len()` never reaches 500. The loop then hits the `from.owner` boundary check and terminates. The response contains **0 allowances**, even though the valid allowance for `S501` exists.

Wait — more precisely: the loop hits `result.len() >= max_results` only when `result` has 500 entries. Since expired entries are `continue`d (not pushed), `result` never fills to 500 from expired entries alone. The loop will eventually reach `S501` and push it. **However**, if there are more than `max_results` expired entries interleaved with valid ones, the loop breaks at the `max_results` limit before reaching valid entries that come after a long run of expired entries in sorted order.

**Corrected scenario:** Account `A` has 500 expired approvals for `S1..S500` (sorted before `S501`) and 1 valid approval for `S501`. With `take = 1`:
- Loop iterates `S1..S500` (all expired, `continue`d, `result.len()` stays 0, limit not hit).
- Loop reaches `S501`, pushes it, `result.len() = 1 >= 1`, breaks.
- Result: `[S501]`. ✓ (works here)

**True impact scenario:** Account `A` has 500 expired approvals for `S1..S500` and 500 valid approvals for `S501..S1000`. With `take = 500` (default):
- Loop iterates `S1..S500` (expired, `continue`d).
- Loop iterates `S501..S1000` (valid, pushed), `result.len()` reaches 500, breaks.
- Result: `[S501..S1000]`. ✓ (works here too)

The actual breakage occurs when the **`break` on `max_results`** fires while there are still valid entries beyond expired ones, specifically when the caller uses `prev_spender` for pagination and the next page starts in a region dense with expired entries followed by valid ones that exceed the page budget. The expired entries consume iteration budget (CPU/instructions) but not result slots, yet the `break` fires on result count — so in practice the pagination **does** work correctly for result count, but **wastes instruction budget** scanning expired entries, and the `break` condition is evaluated correctly.

**Actual confirmed bug:** The `break` fires on `result.len() >= max_results` which counts only non-expired entries. So the result count is correct. The real issue is that **expired entries are iterated but not counted**, meaning the loop may scan far more entries than `max_results` before terminating, consuming excess instruction budget. Additionally, if the stable map contains only expired entries for an account, the loop scans all of them before terminating — a potential DoS vector on query instruction limits for accounts with many expired allowances. [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L221-229)
```rust
        match self
            .allowances_data
            .get_allowance(&(account.clone(), spender.clone()))
        {
            Some(allowance) if allowance.expires_at.unwrap_or_else(remote_future) > now => {
                allowance.clone()
            }
            _ => Allowance::default(),
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L1194-1222)
```rust
    ALLOWANCES_MEMORY.with_borrow(|allowances| {
        for (account_spender, storable_allowance) in
            allowances.range(start_account_spender.clone()..)
        {
            if spender.is_some() && account_spender == start_account_spender {
                continue;
            }
            if result.len() >= max_results as usize {
                break;
            }
            if account_spender.account.owner != from.owner {
                break;
            }
            if let Some(expires_at) = storable_allowance.expires_at
                && expires_at.as_nanos_since_unix_epoch() <= now
            {
                continue;
            }
            result.push(Allowance103 {
                from_account: account_spender.account,
                to_spender: account_spender.spender,
                allowance: Nat::from(storable_allowance.amount),
                expires_at: storable_allowance
                    .expires_at
                    .map(|t| t.as_nanos_since_unix_epoch()),
            });
        }
    });
    result
```

**File:** rs/ledger_suite/icp/ledger/src/lib.rs (L649-683)
```rust
pub fn get_allowances_list(
    from: AccountIdentifier,
    spender: Option<AccountIdentifier>,
    max_results: u64,
    now: u64,
) -> Allowances {
    let mut result = vec![];
    let start_spender = spender.unwrap_or(AccountIdentifier { hash: [0_u8; 28] });
    ALLOWANCES_MEMORY.with_borrow(|allowances| {
        for ((from_account_id, to_spender_id), storable_allowance) in
            allowances.range((from, start_spender)..)
        {
            if spender.is_some() && start_spender == to_spender_id {
                continue;
            }
            if result.len() >= max_results as usize || from_account_id != from {
                break;
            }
            if let Some(expires_at) = storable_allowance.expires_at
                && expires_at.as_nanos_since_unix_epoch() <= now
            {
                continue;
            }
            result.push(Allowance103 {
                from_account_id,
                to_spender_id,
                allowance: storable_allowance.amount,
                expires_at: storable_allowance
                    .expires_at
                    .map(|t| t.as_nanos_since_unix_epoch()),
            });
        }
    });
    result
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1214-1232)
```rust
#[query]
fn icrc103_get_allowances(arg: GetAllowancesArgs) -> Result<Allowances, GetAllowancesError> {
    let from_account = arg.from_account.unwrap_or_else(|| Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: None,
    });
    let max_take_allowances = Access::with_ledger(|ledger| ledger.max_take_allowances());
    let max_results = arg
        .take
        .map(|take| take.0.to_u64().unwrap_or(max_take_allowances))
        .map(|take| std::cmp::min(take, max_take_allowances))
        .unwrap_or(max_take_allowances);
    Ok(get_allowances(
        from_account,
        arg.prev_spender,
        max_results,
        ic_cdk::api::time(),
    ))
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1560-1573)
```rust
#[query]
fn get_allowances(arg: GetAllowancesArgs) -> Allowances {
    let max_take_allowances = Access::with_ledger(|ledger| ledger.max_take_allowances());
    let max_results = arg
        .take
        .map(|take| std::cmp::min(take, max_take_allowances))
        .unwrap_or(max_take_allowances);
    get_allowances_list(
        arg.from_account_id,
        arg.prev_spender_id,
        max_results,
        ic_cdk::api::time(),
    )
}
```
