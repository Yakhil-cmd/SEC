Now I have all the information needed to analyze this vulnerability. Let me examine the critical filtering logic and the ordering semantics.

### Title
Cross-Subaccount Allowance Disclosure in `get_allowances` — (`rs/ledger_suite/icrc1/ledger/src/lib.rs`)

### Summary

The `get_allowances` function terminates its BTreeMap range scan only when the **owner** changes, but never checks whether the **subaccount** of iterated entries matches the queried `from_account`. Because `AccountSpender` keys are ordered by `(owner, effective_subaccount, spender)`, querying with a subaccount that is lexicographically smaller than any stored subaccount causes the scan to silently return allowances belonging to a completely different subaccount of the same owner.

### Finding Description

`get_allowances` in `rs/ledger_suite/icrc1/ledger/src/lib.rs` performs a forward range scan over `ALLOWANCES_MEMORY` starting at the key `(from_account, min_spender)`: [1](#0-0) 

The only loop-termination guard on the account dimension is:

```rust
if account_spender.account.owner != from.owner {
    break;
}
```

There is **no guard** checking `account_spender.account.effective_subaccount() == from.effective_subaccount()`.

`AccountSpender` ordering is `(account.owner, account.effective_subaccount, spender)`: [2](#0-1) 

`Account` ordering uses `effective_subaccount()`, which maps `None` to `[0u8;32]`: [3](#0-2) 

So if owner X has an allowance stored under subaccount `[0xFF;32]` and the attacker queries with subaccount `[0x00;32]` (or `None`), the range `(X, [0x00;32], ∅) ..` includes the key `(X, [0xFF;32], spender_A)`. The loop does not break, and that entry is pushed into the result with `from_account = {owner: X, subaccount: [0xFF;32]}`.

The existing test suite **explicitly acknowledges and accepts** this cross-subaccount bleed-through as expected behavior: [4](#0-3) 

The comment reads: *"If the smaller `from_account` has a different subaccount, the result is the same as listing for current `from_account`."* The assertion `check_allowances(allowances, idx, from.owner)` validates only the owner field, not the full account.

### Impact Explanation

Any unprivileged caller can enumerate **all allowances for all subaccounts of any principal** with a single query:

```
icrc103_get_allowances({
  from_account: Some({ owner: victim_principal, subaccount: None }),
  prev_spender: None,
  take: None
})
```

Because `None` maps to `[0x00;32]` — the lexicographic minimum — the range scan covers every subaccount of `victim_principal`. The response `from_account` field reveals the actual subaccount, so the attacker learns:
- Which subaccounts of the victim have active approvals
- Which spenders are approved and for how much
- Expiry timestamps

This is complete allowance disclosure across all subaccounts of any principal, violating the ICRC-103 invariant that results must only contain entries where `from_account` exactly matches the queried account (owner **and** subaccount).

### Likelihood Explanation

The attack requires no privileges, no tokens, no prior state, and no special timing. It is a single read-only query call to a public endpoint. Any party who knows a victim's principal ID can execute it immediately.

### Recommendation

Add a subaccount equality check inside the loop in `get_allowances`, immediately after the owner check:

```rust
if account_spender.account.owner != from.owner {
    break;
}
// ADD THIS:
if account_spender.account.effective_subaccount() != from.effective_subaccount() {
    break;
}
``` [5](#0-4) 

The test at lines 2836–2847 that currently asserts cross-subaccount bleed-through as correct behavior should also be updated to assert an empty result when the queried subaccount differs from the stored one.

### Proof of Concept

```
1. icrc2_approve(
     from = { owner: X, subaccount: [0xFF;32] },
     spender = A,
     amount = 1_000_000
   )
   → stored key: (X, [0xFF;32], A)

2. icrc103_get_allowances({
     from_account: Some({ owner: X, subaccount: None }),  // effective = [0x00;32]
     prev_spender: None,
     take: None
   })

3. Range scan starts at (X, [0x00;32], ∅).
   First entry found: (X, [0xFF;32], A).
   Owner check: X == X → do not break.
   Subaccount check: MISSING.
   Entry pushed to result with from_account = { owner: X, subaccount: [0xFF;32] }.

4. Returned result contains the allowance for a subaccount the caller never queried.
   Invariant violated: result[0].from_account ≠ queried from_account.
```

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

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L2836-2847)
```rust
            // List from a smaller `from_account`. If the smaller `from_account` has a different owner
            // the result list is empty - we don't have any approvals for that owner.
            // If the smaller `from_account` has a different subaccount, the result is the same
            // as listing for current `from_account` - the smaller subaccount does not match any account we generated.
            args.from_account = Some(prev_account(&from));
            let allowances = list_allowances(&env, canister_id, from.owner, args.clone())
                .expect("failed to list allowances");
            if args.from_account.unwrap().owner == from.owner {
                check_allowances(allowances, idx, from.owner);
            } else {
                assert_eq!(allowances.len(), 0);
            }
```
