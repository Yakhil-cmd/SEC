The vulnerability is real and concrete. Let me trace through the exact code path.

**The root cause is in `get_allowances`** at `rs/ledger_suite/icrc1/ledger/src/lib.rs` lines 1194–1220. The loop iterates over a BTreeMap range starting from `start_account_spender` and breaks only when `account_spender.account.owner != from.owner` — it **never checks the subaccount**.

`AccountSpender` ordering is: `account.owner` → `account.effective_subaccount()` → `spender`. So the BTreeMap stores entries like:

```
(owner: X, subaccount: [0x00;32], spender: ...) ← range start (queried)
(owner: X, subaccount: [0x01;32], spender: ...)
...
(owner: X, subaccount: [0xFF;32], spender: A)   ← actually stored, returned!
```

The range `start_account_spender..` includes all entries for owner X with any subaccount ≥ `[0x00;32]`, and the only break guard is `owner != from.owner` — the subaccount is never compared.

---

### Title
`get_allowances` leaks allowances across all subaccounts of a principal due to missing subaccount equality check — (`rs/ledger_suite/icrc1/ledger/src/lib.rs`)

### Summary
`icrc103_get_allowances` iterates a BTreeMap range anchored at the queried `from_account` but only breaks on `owner` mismatch, never on `subaccount` mismatch. Any caller can query with `{owner: X, subaccount: [0x00;32]}` and receive all allowances for every subaccount of X.

### Finding Description
In `get_allowances`, the loop guard is:

```rust
if account_spender.account.owner != from.owner {
    break;
}
``` [1](#0-0) 

There is no corresponding check `account_spender.account != from` (or `account_spender.account.effective_subaccount() != from.effective_subaccount()`). Because `AccountSpender` is ordered by `(owner, subaccount, spender)`: [2](#0-1) 

and `Account::cmp` uses `effective_subaccount()`: [3](#0-2) 

a range starting at `(owner: X, subaccount: [0x00;32], spender: ∅)` will include every stored entry `(owner: X, subaccount: S, spender: *)` for any `S ≥ [0x00;32]`. Since `[0x00;32]` is the minimum possible subaccount, querying with it returns **all** allowances for all subaccounts of owner X.

The `icrc103_get_allowances` entrypoint accepts any `from_account` from any caller with no access control: [4](#0-3) 

### Impact Explanation
An unprivileged attacker can enumerate every active allowance for every subaccount of any target principal by calling `icrc103_get_allowances({from_account: {owner: X, subaccount: [0x00;32]}, prev_spender: None, take: None})`. The returned `Allowance103` records will have `from_account` fields pointing to subaccounts the caller never queried, violating the invariant that results must match the queried account exactly. This enables targeted discovery of high-value approvals (e.g., large DeFi positions held in specific subaccounts) across the entire principal.

### Likelihood Explanation
The attack requires only a single public query call with a crafted `from_account`. No tokens, no approvals, no privileged role needed. The endpoint is a `#[query]` callable by anyone.

### Recommendation
Add a subaccount equality guard inside the loop, immediately after the owner check:

```rust
if account_spender.account.owner != from.owner {
    break;
}
// ADD THIS:
if account_spender.account != from {
    break;
}
``` [5](#0-4) 

### Proof of Concept
1. Owner X calls `icrc2_approve(from={owner:X, subaccount:[0xFF;32]}, spender:A, amount:1_000_000)`.
2. Attacker (any principal) calls `icrc103_get_allowances({from_account:{owner:X, subaccount:[0x00;32]}, prev_spender:None, take:None})`.
3. The BTreeMap range starts at `(X, [0x00;32], ∅)`. The stored entry `(X, [0xFF;32], A)` is ≥ this start and owner X == X, so the loop does not break.
4. The response contains `Allowance103 { from_account: {owner:X, subaccount:[0xFF;32]}, to_spender: A, allowance: 1_000_000 }` — a different subaccount than queried.
5. Invariant assertion `∀ a ∈ response: a.from_account == queried_from_account` fails.

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L372-378)
```rust
impl std::cmp::Ord for AccountSpender {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.account
            .cmp(&other.account)
            .then_with(|| self.spender.cmp(&other.spender))
    }
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

**File:** packages/icrc-ledger-types/src/icrc1/account.rs (L54-60)
```rust
impl std::cmp::Ord for Account {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.owner.cmp(&other.owner).then_with(|| {
            self.effective_subaccount()
                .cmp(other.effective_subaccount())
        })
    }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1215-1231)
```rust
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
```
