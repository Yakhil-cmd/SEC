Now I have all the information needed to analyze this. Let me trace the exact code path.

### Title
Subaccount Scope Bypass in `get_allowances` Leaks Cross-Subaccount Allowance Data — (`rs/ledger_suite/icrc1/ledger/src/lib.rs`)

---

### Summary

The `get_allowances` function terminates its BTreeMap range scan only when the iterated entry's `account.owner` differs from the requested `from.owner`. It never checks whether `account.subaccount` matches. When a caller supplies a specific subaccount in `from_account`, the function silently returns allowances belonging to every other subaccount of the same owner that is lexicographically greater than the requested one.

---

### Finding Description

`icrc103_get_allowances` (no access control, open `#[query]`) passes the caller-supplied `from_account` directly to `get_allowances`: [1](#0-0) 

Inside `get_allowances`, the range scan starts at the exact `(from_account, min_spender)` key and iterates forward through the `StableBTreeMap`. The only termination guard on the `from` dimension is: [2](#0-1) 

This checks `account_spender.account.owner != from.owner` — **owner only**. It never compares `account_spender.account.subaccount` against `from.subaccount`.

The `AccountSpender` ordering is lexicographic on `(account.owner, account.effective_subaccount(), spender.owner, spender.effective_subaccount())`: [3](#0-2) [4](#0-3) 

So for `from = {owner: V, subaccount: Some([1;32])}`, the scan starts at the first entry whose account is `(V, [1;32])` and continues through `(V, [2;32])`, `(V, [3;32])`, …, `(V, [255;32])` — all subaccounts of `V` that sort after `[1;32]` — before finally hitting a different owner and breaking. Every one of those entries is appended to the result.

---

### Impact Explanation

Any unprivileged principal can enumerate the full set of allowances (spender identities + approved amounts + expiry times) for every subaccount of a victim principal whose subaccount sorts after the one supplied in `from_account`. By sweeping `from_account.subaccount` from `[0;32]` upward, an attacker can reconstruct the complete allowance table for any principal. This is a direct privacy/confidentiality violation: allowance data is supposed to be readable only by the approver or the spender, yet this endpoint exposes it to arbitrary third parties.

---

### Likelihood Explanation

The endpoint is a public `#[query]` with no caller authentication or authorization check. Any IC principal can call it with an arbitrary `from_account`. The exploit requires a single canister call and is deterministically reproducible in a state-machine test.

---

### Recommendation

Replace the owner-only guard with a full `Account` equality check (owner **and** effective subaccount):

```rust
// lib.rs  get_allowances(), inside the range loop
if account_spender.account != from {   // was: .owner != from.owner
    break;
}
```

`Account::eq` already compares both `owner` and `effective_subaccount()`: [5](#0-4) 

This single-character change confines the scan to the exact `(owner, subaccount)` pair the caller requested.

---

### Proof of Concept

```rust
// State-machine test sketch
let victim = Principal::from_slice(&[1u8; 2]);
let sub1: Subaccount = [1u8; 32];
let sub2: Subaccount = [2u8; 32];
let spender_a = Account { owner: Principal::from_slice(&[10u8; 2]), subaccount: None };
let spender_b = Account { owner: Principal::from_slice(&[11u8; 2]), subaccount: None };

// Victim approves from two different subaccounts
approve(&env, ledger, victim, sub1, spender_a, 1_000);
approve(&env, ledger, victim, sub2, spender_b, 2_000);

// Attacker (any principal) queries only sub1
let args = GetAllowancesArgs {
    from_account: Some(Account { owner: victim, subaccount: Some(sub1) }),
    prev_spender: None,
    take: None,
};
let result = icrc103_get_allowances(&env, ledger, attacker, args).unwrap();

// BUG: both sub1 and sub2 allowances are returned
assert_eq!(result.len(), 2);   // should be 1
assert!(result.iter().any(|a| a.from_account.subaccount == Some(sub2)));  // leaked
```

The scan starts at `(victim, [1;32], min_spender)`, passes through `(victim, [1;32], spender_a)` (correct), then continues to `(victim, [2;32], spender_b)` (leaked) because the break guard only tests `account.owner`, not `account.subaccount`. [6](#0-5)

### Citations

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

**File:** packages/icrc-ledger-types/src/icrc1/account.rs (L40-43)
```rust
impl PartialEq for Account {
    fn eq(&self, other: &Self) -> bool {
        self.owner == other.owner && self.effective_subaccount() == other.effective_subaccount()
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
