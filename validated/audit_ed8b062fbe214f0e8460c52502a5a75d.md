The vulnerability is real and concrete. Let me confirm the exact mechanics before writing the finding.

The `AccountSpender` `Ord` implementation orders by `account` first (which itself orders by `owner`, then `effective_subaccount()`), then by `spender`.

### Title
Cross-Subaccount Allowance Enumeration via Incomplete Break Condition in `get_allowances` - (`rs/ledger_suite/icrc1/ledger/src/lib.rs`)

---

### Summary

The `get_allowances` function iterates a `StableBTreeMap` ordered by `(account, spender)` and breaks only when `account_spender.account.owner != from.owner`. Because `Account` ordering is `(owner, effective_subaccount)`, a query for `{owner:X, subaccount:Some([0u8;32])}` will also return allowances stored under `{owner:X, subaccount:Some([1u8;32])}`, `{owner:X, subaccount:Some([2u8;32])}`, etc. — any subaccount that sorts lexicographically after the queried one. Any unprivileged caller can trigger this via the public `icrc103_get_allowances` query endpoint with no authentication required.

---

### Finding Description

**BTreeMap key ordering.** `AccountSpender::Ord` orders first by `account`, then by `spender`: [1](#0-0) 

`Account::Ord` orders by `owner` first, then by `effective_subaccount()`: [2](#0-1) 

This means the BTreeMap stores entries in the order:
```
{owner:X, subaccount:[0;32]} -> spender_A
{owner:X, subaccount:[0;32]} -> spender_B
{owner:X, subaccount:[1;32]} -> spender_C   ← different subaccount, same owner
{owner:X, subaccount:[2;32]} -> spender_D   ← different subaccount, same owner
{owner:Y, ...}                               ← different owner, loop breaks here
```

**The defective loop.** `get_allowances` starts a range scan at `start_account_spender` (constructed from the exact queried `from` account) and breaks only on owner mismatch: [3](#0-2) 

The condition `account_spender.account.owner != from.owner` is `false` for all subaccounts of the same owner, so the loop continues past the queried subaccount boundary and collects allowances belonging to other subaccounts of X.

**No access control on the endpoint.** `icrc103_get_allowances` accepts an arbitrary `from_account` from any caller with no authentication: [4](#0-3) 

The endpoint is a public `#[query]` callable by any principal.

---

### Impact Explanation

An unprivileged attacker can enumerate, for any principal X:
- Which subaccounts of X have active allowances
- The identity of every spender approved by those subaccounts
- The exact allowance amounts and expiration timestamps

This violates the ICRC-103 invariant that `icrc103_get_allowances` must return only allowances where `from_account` exactly matches the queried account (owner **and** effective subaccount). The exposure is limited to allowance metadata — no funds can be moved — but it is a concrete, unauthenticated information-disclosure vulnerability affecting any ledger deploying this code.

---

### Likelihood Explanation

Exploitation requires only a single public query call with a crafted `from_account`. No privileged access, no key material, no social engineering. The attacker needs only to know (or guess) a principal X that has approved from multiple subaccounts, which is observable from on-chain transaction history via ICRC-3.

---

### Recommendation

Replace the owner-only break condition with a full `Account` comparison:

```rust
// Before (line 1204):
if account_spender.account.owner != from.owner {
    break;
}

// After:
if account_spender.account != from {
    break;
}
```

`Account::PartialEq` already compares both `owner` and `effective_subaccount()`: [5](#0-4) 

This single-character change is sufficient because the BTreeMap range already starts at the correct `from` account, so the first entry that differs in subaccount will immediately trigger the break.

---

### Proof of Concept

```
State setup (as owner X):
  icrc2_approve(from_subaccount=[1u8;32], spender=S1, amount=100)
  icrc2_approve(from_subaccount=[2u8;32], spender=S2, amount=200)

Attack (as any principal, including anonymous):
  icrc103_get_allowances({
    from_account: Some({owner: X, subaccount: Some([0u8;32])}),
    prev_spender: None,
    take: None
  })

Expected (correct) result: [] (no allowances for subaccount [0;32])

Actual (buggy) result:
  [
    { from_account: {owner:X, subaccount:[1u8;32]}, to_spender: S1, allowance: 100 },
    { from_account: {owner:X, subaccount:[2u8;32]}, to_spender: S2, allowance: 200 }
  ]
```

The range scan starts at `{account:{owner:X, subaccount:[0;32]}, spender:anonymous}`. Since `[0u8;32] < [1u8;32] < [2u8;32]` lexicographically, both other subaccounts' entries fall within the range. The break condition `owner != from.owner` is never satisfied for them, so they are returned to the attacker.

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

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L1194-1206)
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
```

**File:** packages/icrc-ledger-types/src/icrc1/account.rs (L40-44)
```rust
impl PartialEq for Account {
    fn eq(&self, other: &Self) -> bool {
        self.owner == other.owner && self.effective_subaccount() == other.effective_subaccount()
    }
}
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
