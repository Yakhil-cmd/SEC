### Title
Cross-Subaccount Allowance Leakage in `get_allowances` Due to Incomplete Boundary Check — (`rs/ledger_suite/icrc1/ledger/src/lib.rs`)

---

### Summary

The `get_allowances` function uses an owner-only boundary check (`account_spender.account.owner != from.owner`) to terminate BTreeMap iteration, but the map is ordered by full `Account` (owner + subaccount). This means iteration continues past the queried subaccount into other subaccounts of the same owner, leaking their allowances into the response.

---

### Finding Description

In `get_allowances` (line 1174), the loop iterates over `ALLOWANCES_MEMORY` — a `StableBTreeMap<AccountSpender, StorableAllowance>` — starting from the queried `from` account:

```
allowances.range(start_account_spender.clone()..)
```

The loop body applies checks in this order:

1. **Line 1198**: Skip the exact start entry (pagination cursor)
2. **Line 1201**: `if result.len() >= max_results as usize { break; }` — size cap
3. **Line 1204**: `if account_spender.account.owner != from.owner { break; }` — boundary check [1](#0-0) 

The `AccountSpender` ordering delegates to `Account::cmp`:

```rust
fn cmp(&self, other: &Self) -> std::cmp::Ordering {
    self.owner.cmp(&other.owner).then_with(|| {
        self.effective_subaccount().cmp(other.effective_subaccount())
    })
}
``` [2](#0-1) 

And `AccountSpender::cmp` orders by `(account, spender)`: [3](#0-2) 

So the BTreeMap is ordered by `(owner, subaccount, spender_owner, spender_subaccount)`. When iterating from `{owner:X, subaccount:[0;32]}`, after exhausting all entries for that exact account, the iterator naturally advances to `{owner:X, subaccount:[1;32]}`. The boundary check at line 1204 only tests `account_spender.account.owner != from.owner` — which evaluates to `false` (same owner X), so the loop does **not** break. Entries belonging to `{owner:X, subaccount:[1;32]}` are pushed into `result` with `from_account` set to `{X, [1;32]}`, not the queried `{X, [0;32]}`. [4](#0-3) [5](#0-4) 

The fix is to change line 1204 from:
```rust
if account_spender.account.owner != from.owner {
```
to:
```rust
if account_spender.account != from {
```

---

### Impact Explanation

Any caller of `icrc103_get_allowances` who queries `from_account = {owner:X, subaccount:SA}` will receive allowances that belong to other subaccounts of owner X (those that sort after SA in the BTreeMap). The returned `Allowance` entries will have `from_account.owner == X` but `from_account.subaccount != SA`, violating the per-account isolation guarantee of ICRC-103. This is an information-disclosure vulnerability: allowance amounts, spender identities, and expiry times for unqueried subaccounts are exposed.

---

### Likelihood Explanation

The exploit requires no privilege. It is a deterministic, locally reproducible query call. Any principal who knows (or guesses) that owner X has allowances from multiple subaccounts can trigger it. The precondition — owner X having approved from two different subaccounts — is a normal, expected usage pattern.

---

### Recommendation

Replace the owner-only boundary check with a full-account equality check:

```rust
// Line 1204 — change from:
if account_spender.account.owner != from.owner {
// to:
if account_spender.account != from {
```

`Account::eq` already uses `effective_subaccount()` for comparison (line 42 of `account.rs`), so `None` and `Some([0;32])` are treated as equal, which is the correct semantic. [6](#0-5) 

---

### Proof of Concept

```
1. icrc2_approve: caller={X, [0;32]}, spender=S1, amount=100
2. icrc2_approve: caller={X, [1;32]}, spender=S2, amount=200
3. icrc103_get_allowances(from_account={X, [0;32]}, prev_spender=None, take=Some(500))
```

Expected response: only `[{from_account:{X,[0;32]}, to_spender:S1, allowance:100}]`

Actual response (with the bug): also includes `{from_account:{X,[1;32]}, to_spender:S2, allowance:200}` because after iterating past all entries for `{X,[0;32]}`, the iterator reaches `{X,[1;32]}` and the check `account_spender.account.owner != from.owner` is `false` (both are X), so the entry is appended to `result`.

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

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L1194-1221)
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
```

**File:** packages/icrc-ledger-types/src/icrc1/account.rs (L40-44)
```rust
impl PartialEq for Account {
    fn eq(&self, other: &Self) -> bool {
        self.owner == other.owner && self.effective_subaccount() == other.effective_subaccount()
    }
}
```

**File:** packages/icrc-ledger-types/src/icrc1/account.rs (L54-61)
```rust
impl std::cmp::Ord for Account {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.owner.cmp(&other.owner).then_with(|| {
            self.effective_subaccount()
                .cmp(other.effective_subaccount())
        })
    }
}
```
