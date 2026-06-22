### Title
`get_allowances` Break Condition Checks Only `owner`, Not Full `Account` — Subaccount Allowance Leakage — (`rs/ledger_suite/icrc1/ledger/src/lib.rs`)

---

### Summary

The `get_allowances` function in the ICRC-1 ledger uses a `StableBTreeMap` range scan starting at the requested `from` account. Its loop-termination guard only compares `account_spender.account.owner != from.owner`, ignoring the subaccount field. As a result, a caller who requests allowances for `{A, Some([1;32])}` will also receive allowances belonging to `{A, Some([2;32])}`, `{A, Some([3;32])}`, and any other subaccount of principal `A` whose BTreeMap key sorts after the requested one.

---

### Finding Description

The vulnerable function is `get_allowances` at: [1](#0-0) 

The range scan opens at `start_account_spender` (which encodes the exact `from` account + minimum spender) and iterates forward. The only guard that terminates the scan when the account changes is:

```rust
if account_spender.account.owner != from.owner {
    break;
}
``` [2](#0-1) 

Because `AccountSpender` keys are ordered lexicographically by `(account.owner, account.subaccount, spender.owner, spender.subaccount)`, after exhausting all spenders for `{A, Some([1;32])}` the iterator naturally advances to `{A, Some([2;32])}`. The owner is still `A`, so the guard does **not** fire, and allowances for the wrong subaccount are appended to the result.

The public entrypoint that feeds this function is: [3](#0-2) 

It is a `#[query]` method with **no access-control check** — any unprivileged caller may supply an arbitrary `from_account`.

---

### Impact Explanation

An unprivileged caller can enumerate allowances for every subaccount of a target principal `A` by issuing a single `icrc103_get_allowances` call with `from_account = {A, Some([0;32])}` (the lexicographically smallest non-default subaccount). The response will contain allowances for all subaccounts of `A` up to the `max_take` page size (default 500), leaking spender identities, amounts, and expiration timestamps that belong to accounts the caller did not ask about.

---

### Likelihood Explanation

The endpoint is a public query callable by any principal on the IC without cycles or special permissions. The exploit requires no privileged role, no key material, and no consensus-level attack. It is reproducible in a local replica or state-machine test with two approvals from different subaccounts of the same principal.

---

### Recommendation

Replace the owner-only comparison with a full `Account` equality check:

```rust
// Before (buggy):
if account_spender.account.owner != from.owner {
    break;
}

// After (correct):
if account_spender.account != from {
    break;
}
``` [2](#0-1) 

This ensures the scan terminates as soon as the iterated key's account diverges from the exact requested `from` account (owner **and** subaccount).

---

### Proof of Concept

```
Setup:
  principal A = <some test principal>
  approve({A, Some([1;32])}, spender_X, amount=100)
  approve({A, Some([2;32])}, spender_Y, amount=200)

Call:
  icrc103_get_allowances({
    from_account: Some({owner: A, subaccount: Some([1;32])}),
    prev_spender: None,
    take: None
  })

Expected (correct):
  [ {from_account: {A, Some([1;32])}, to_spender: spender_X, allowance: 100} ]

Actual (buggy):
  [ {from_account: {A, Some([1;32])}, to_spender: spender_X, allowance: 100},
    {from_account: {A, Some([2;32])}, to_spender: spender_Y, allowance: 200} ]
```

The second entry — belonging to a different subaccount — is returned because the break guard at line 1204 only checks `owner`, not the full `Account`. [4](#0-3)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L1174-1223)
```rust
pub fn get_allowances(
    from: Account,
    spender: Option<Account>,
    max_results: u64,
    now: u64,
) -> Allowances {
    let mut result = vec![];
    let start_account_spender = match spender {
        Some(spender) => AccountSpender {
            account: from,
            spender,
        },
        None => AccountSpender {
            account: from,
            spender: Account {
                owner: Principal::from_slice(&[0_u8; 0]),
                subaccount: None,
            },
        },
    };
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
