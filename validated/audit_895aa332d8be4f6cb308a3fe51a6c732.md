### Title
Cross-Subaccount Allowance Enumeration via Incomplete Owner-Only Boundary Check in `get_allowances` — (`rs/ledger_suite/icrc1/ledger/src/lib.rs`)

---

### Summary

The `get_allowances` function in the ICRC-1 ledger iterates over a `StableBTreeMap` ordered by `(account.owner, account.effective_subaccount, spender)`, but the loop termination guard at line 1204 only checks `account_spender.account.owner != from.owner`, ignoring the subaccount field. As a result, a caller who supplies `from_account = (P, sub1)` will receive allowances belonging to `(P, sub2)`, `(P, sub3)`, etc. — every subaccount of principal `P` that sorts after `sub1` — until a different principal is encountered.

---

### Finding Description

**Entrypoint:** `icrc103_get_allowances` is a public `#[query]` endpoint with no authentication requirement. [1](#0-0) 

The caller-supplied `from_account` is passed directly to `get_allowances`: [2](#0-1) 

Inside `get_allowances`, the `ALLOWANCES_MEMORY` `StableBTreeMap` is keyed by `AccountSpender`, whose `Ord` implementation sorts first by the full `Account` (owner then `effective_subaccount`), then by spender: [3](#0-2) 

`Account::cmp` uses `effective_subaccount()`, which normalises `None` to `[0u8; 32]`, so the map is ordered by `(owner, subaccount_bytes, spender_owner, spender_subaccount_bytes)`: [4](#0-3) 

The iteration starts at `(from, min_spender)` and the only guard that can terminate it is: [5](#0-4) 

Because `account_spender.account.owner` equals `from.owner` for **every** subaccount of principal `P`, the loop never breaks when it crosses from `(P, sub1, ...)` into `(P, sub2, ...)`. All allowances for all subaccounts of `P` that sort after `sub1` are returned.

---

### Impact Explanation

Any unprivileged caller can:

1. Call `icrc103_get_allowances` with `from_account = (P, [0u8; 32])` (the lexicographically smallest subaccount).
2. Receive up to `max_take_allowances` (default 500) allowances, spanning every subaccount of `P`.
3. Paginate using `prev_spender` to exhaust the full set.

The response leaks:
- The exact subaccount from which each approval was granted (`from_account` field in each `Allowance103`).
- The spender identity and approved amount for each allowance.

This is a privacy violation: subaccount usage patterns and financial relationships across all subaccounts of a principal are exposed to any observer.

---

### Likelihood Explanation

- The endpoint is a public query, callable by any principal with no fee or privilege.
- The exploit requires only a single well-formed query call; no on-chain transaction is needed.
- The `from_account` field accepts any `Account` value — the caller is not restricted to their own principal.

---

### Recommendation

Change the boundary check at line 1204 from comparing only the owner to comparing the full `Account` (which already implements `PartialEq` using `effective_subaccount`):

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

`Account::eq` already normalises `None` and `Some([0u8; 32])` identically via `effective_subaccount()`, so this change preserves the existing behaviour for the default-subaccount equivalence tested in the existing test suite, while correctly scoping enumeration to the exact `(owner, subaccount)` pair. [6](#0-5) 

---

### Proof of Concept

State-machine test sketch (using the existing test harness):

```rust
// Setup: principal P with two distinct subaccounts
let sub1 = [1u8; 32];
let sub2 = [2u8; 32];
let account1 = Account { owner: P, subaccount: Some(sub1) };
let account2 = Account { owner: P, subaccount: Some(sub2) };
let spender_a = Account { owner: SPENDER_A, subaccount: None };
let spender_b = Account { owner: SPENDER_B, subaccount: None };

// Create approvals from two different subaccounts of P
send_approval(&env, ledger, P, &ApproveArgs { from_subaccount: Some(sub1), spender: spender_a, amount: 100.into(), .. });
send_approval(&env, ledger, P, &ApproveArgs { from_subaccount: Some(sub2), spender: spender_b, amount: 200.into(), .. });

// Query with from_account = (P, sub1) — should return ONLY sub1's allowance
let args = GetAllowancesArgs { from_account: Some(account1), prev_spender: None, take: None };
let allowances = list_allowances(&env, ledger, ATTACKER, args).unwrap();

// BUG: allowances.len() == 2, containing both sub1 and sub2 allowances
// EXPECTED: allowances.len() == 1, containing only sub1's allowance
assert_eq!(allowances.len(), 1);  // This assertion FAILS with the current code
assert!(allowances.iter().all(|a| a.from_account == account1));
```

The existing test suite does not cover this case — all existing tests use either the same effective subaccount (`None` vs `Some([0u8; 32])`) or a single subaccount per principal. [7](#0-6)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1214-1231)
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

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L1204-1206)
```rust
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

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L2985-3079)
```rust
{
    let approver_none = Account {
        owner: PrincipalId::new_user_test_id(1).0,
        subaccount: None,
    };
    let approver_default = Account {
        owner: PrincipalId::new_user_test_id(2).0,
        subaccount: Some(*DEFAULT_SUBACCOUNT),
    };
    let initial_balances = vec![(approver_none, 100_000), (approver_default, 100_000)];
    let spender_none = Account {
        owner: PrincipalId::new_user_test_id(3).0,
        subaccount: None,
    };
    let spender_default = Account {
        owner: PrincipalId::new_user_test_id(3).0,
        subaccount: Some(*DEFAULT_SUBACCOUNT),
    };

    let (env, canister_id) = setup(ledger_wasm, encode_init_args, initial_balances);

    let approve_args = default_approve_args(spender_none, 1);
    let block_index = send_approval(&env, canister_id, approver_none.owner, &approve_args)
        .expect("approval failed");
    assert_eq!(block_index, 2);

    let mut approve_args = default_approve_args(spender_default, 1);
    approve_args.from_subaccount = approver_default.subaccount;
    let block_index = send_approval(&env, canister_id, approver_default.owner, &approve_args)
        .expect("approval failed");
    assert_eq!(block_index, 3);

    // Should return the allowance, if we specify `from_account` as when creating approval
    let args = GetAllowancesArgs {
        from_account: Some(approver_none),
        prev_spender: None,
        take: None,
    };
    let allowances = list_allowances(&env, canister_id, approver_none.owner, args.clone())
        .expect("failed to list allowances");
    assert_eq!(allowances.len(), 1);

    // Should return the allowance, if we specify `from_account` with explicit default subaccount.
    let mut approver_none_default = approver_none;
    approver_none_default.subaccount = Some(*DEFAULT_SUBACCOUNT);
    let args = GetAllowancesArgs {
        from_account: Some(approver_none_default),
        prev_spender: None,
        take: None,
    };
    let allowances = list_allowances(&env, canister_id, approver_none.owner, args.clone())
        .expect("failed to list allowances");
    assert_eq!(allowances.len(), 1);

    // Should filter out the allowance if subaccount is none
    let args = GetAllowancesArgs {
        from_account: Some(approver_none),
        prev_spender: Some(spender_none),
        take: None,
    };
    let allowances = list_allowances(&env, canister_id, approver_none.owner, args.clone())
        .expect("failed to list allowances");
    assert_eq!(allowances.len(), 0);

    // Should filter out the allowance if subaccount is default
    let args = GetAllowancesArgs {
        from_account: Some(approver_none),
        prev_spender: Some(spender_default),
        take: None,
    };
    let allowances = list_allowances(&env, canister_id, approver_none.owner, args.clone())
        .expect("failed to list allowances");
    assert_eq!(allowances.len(), 0);

    // Should return the allowance, if we specify `from_account` as when creating approval
    let args = GetAllowancesArgs {
        from_account: Some(approver_default),
        prev_spender: None,
        take: None,
    };
    let allowances = list_allowances(&env, canister_id, approver_default.owner, args.clone())
        .expect("failed to list allowances");
    assert_eq!(allowances.len(), 1);

    // Should return the allowance, if we specify `from_account` with none subaccount.
    let mut approver_default_none = approver_default;
    approver_default_none.subaccount = None;
    let args = GetAllowancesArgs {
        from_account: Some(approver_default_none),
        prev_spender: None,
        take: None,
    };
    let allowances = list_allowances(&env, canister_id, approver_default.owner, args)
        .expect("failed to list allowances");
    assert_eq!(allowances.len(), 1);
```
