The code at line 1204 is the crux of the question. Let me show exactly what it does: [1](#0-0) 

The `get_allowances` function iterates a `BTreeMap` (`ALLOWANCES_MEMORY`) starting from `start_account_spender`, which is constructed using the full `from` account (owner + subaccount): [2](#0-1) 

The break condition at line 1204 is:

```rust
if account_spender.account.owner != from.owner {
    break;
}
```

This only compares `owner` (a `Principal`), **not** the full `Account` (which includes `subaccount`). Because the `BTreeMap` is ordered lexicographically by `(owner, subaccount, spender)`, iterating from `(owner=X, subaccount=[1u8;32], spender=min)` will naturally traverse into entries for `(owner=X, subaccount=[2u8;32], spender=*)` without triggering the break — the owner is still `X`.

The correct guard should be:
```rust
if account_spender.account != from {
    break;
}
```

---

### Verdict

### Title
`get_allowances` Break Condition Ignores Subaccount, Leaking Sibling Subaccount Allowances — (`rs/ledger_suite/icrc1/ledger/src/lib.rs`)

### Summary
`get_allowances` (called by `icrc103_get_allowances`) iterates the allowance BTreeMap and stops only when the `owner` field changes. It does not stop when the `subaccount` field changes. A caller requesting allowances for `(owner=X, subaccount=[1u8;32])` will also receive allowances belonging to `(owner=X, subaccount=[2u8;32])`, `(owner=X, subaccount=[3u8;32])`, etc.

### Finding Description
In `rs/ledger_suite/icrc1/ledger/src/lib.rs`, the `get_allowances` function: [3](#0-2) 

The loop at line 1195 ranges over the stable BTreeMap starting from `start_account_spender`. The only termination guard for "wrong account" is:

```rust
if account_spender.account.owner != from.owner {
    break;
}
```

Because `Account = { owner: Principal, subaccount: Option<[u8;32]> }` and the map is ordered by the full key `(owner, subaccount, spender)`, entries for `(X, subaccount=[2u8;32])` sort immediately after entries for `(X, subaccount=[1u8;32])`. The break condition never fires for them, so they are collected into `result` and returned to the caller.

The `GetAllowancesArgs` type confirms `from_account` is a full `Account` (owner + optional subaccount): [4](#0-3) 

### Impact Explanation
Any unprivileged caller can enumerate the complete allowance map for every subaccount of any principal `X` by:
1. Calling `icrc103_get_allowances({ from_account: {owner: X, subaccount: Some([0u8;32])}, prev_spender: None, take: None })`.
2. Paginating through results — the loop never breaks on subaccount boundaries, so all subaccounts' allowances are returned in BTreeMap order.

This discloses: which subaccounts exist, which spenders are approved, approved amounts, and expiry times — for every subaccount of any target principal. This is a privacy/information-disclosure vulnerability. The attacker does not gain the ability to execute `transfer_from` directly (they still need to be the approved spender), but the disclosure enables targeted exploitation of high-value approvals that would otherwise be unknown.

### Likelihood Explanation
The endpoint is a public query callable by any unprivileged principal with no authentication requirement. The exploit requires a single canister query call. It is trivially reproducible in a state-machine test.

### Recommendation
Change the break condition from:
```rust
if account_spender.account.owner != from.owner {
    break;
}
```
to:
```rust
if account_spender.account != from {
    break;
}
```

This ensures the iteration stops as soon as the full account (owner + subaccount) no longer matches the requested `from` account.

### Proof of Concept
State-machine test sketch:
1. `icrc2_approve(from={owner:X, subaccount:Some([1u8;32])}, spender:A, amount:100)`
2. `icrc2_approve(from={owner:X, subaccount:Some([2u8;32])}, spender:B, amount:200)`
3. Call `icrc103_get_allowances({from_account:{owner:X, subaccount:Some([1u8;32])}, prev_spender:None, take:None})`
4. Assert: result contains **only** the allowance for `(X, [1u8;32]) → A`.
5. Observe: result **also** contains the allowance for `(X, [2u8;32]) → B`, violating the invariant. [5](#0-4)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L1174-1222)
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
```

**File:** packages/icrc-ledger-types/src/icrc103/get_allowances.rs (L7-12)
```rust
#[derive(CandidType, Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct GetAllowancesArgs {
    pub from_account: Option<Account>,
    pub prev_spender: Option<Account>,
    pub take: Option<Nat>,
}
```
