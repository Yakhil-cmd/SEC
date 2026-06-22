### Title
Missing Access Control in `icrc103_get_allowances` Allows Any Caller to Enumerate All Allowances for Any Account - (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

### Summary

The `icrc103_get_allowances` query endpoint accepts an arbitrary `from_account` parameter and unconditionally returns all active allowances for that account to any caller, including anonymous principals. The `GetAllowancesError::AccessDenied` variant is defined in the type system but is never constructed or returned anywhere in the codebase, making the access control mechanism a dead letter.

### Finding Description

The handler at `rs/ledger_suite/icrc1/ledger/src/main.rs` line 1215 is:

```rust
#[query]
fn icrc103_get_allowances(arg: GetAllowancesArgs) -> Result<Allowances, GetAllowancesError> {
    let from_account = arg.from_account.unwrap_or_else(|| Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: None,
    });
    // ... no caller check against from_account.owner ...
    Ok(get_allowances(
        from_account,
        arg.prev_spender,
        max_results,
        ic_cdk::api::time(),
    ))
}
``` [1](#0-0) 

When `from_account` is `Some(victim_account)`, the caller's identity (`msg_caller()`) is never compared against `from_account.owner`. The function proceeds directly to `Ok(get_allowances(...))` with no guard.

The `GetAllowancesError::AccessDenied` variant is defined:

```rust
pub enum GetAllowancesError {
    AccessDenied { reason: String },
    GenericError { error_code: Nat, message: String },
}
``` [2](#0-1) 

But a codebase-wide search confirms `AccessDenied` is constructed **zero times** in production ledger code — only the type definition itself appears in `get_allowances.rs` and `ledger.did`. [3](#0-2) 

The underlying `get_allowances` helper iterates the stable `ALLOWANCES_MEMORY` B-tree for all entries keyed by `from_account`, returning spender principal, subaccount, allowance amount, and expiry for every active approval: [4](#0-3) 

### Impact Explanation

Any caller — including the anonymous principal — can call:

```
icrc103_get_allowances(
  from_account = Some({ owner: victim_principal, subaccount: None }),
  prev_spender  = None,
  take          = None
)
```

and receive the complete list of: every spender principal that `victim_principal` has approved, the exact allowance amount granted to each spender, and each allowance's expiry timestamp.

This differs materially from `icrc2_allowance`, which requires the caller to already know a specific `(account, spender)` pair. `icrc103_get_allowances` enables **discovery** — an attacker learns which spenders exist without any prior knowledge, enabling targeted social engineering or front-running of `transfer_from` operations.

No funds can be directly stolen through this call alone, but the exposure of the full approval graph for any account is a concrete privacy and financial-metadata leak.

### Likelihood Explanation

The endpoint is a public `#[query]` call, reachable by any ingress message or inter-canister query. No special role, key, or governance action is required. The call is free (query calls consume no cycles from the caller). Exploitation requires only knowledge of a victim's principal, which is often public on-chain.

### Recommendation

Add a caller identity check before returning data for a foreign account:

```rust
#[query]
fn icrc103_get_allowances(arg: GetAllowancesArgs) -> Result<Allowances, GetAllowancesError> {
    let caller = ic_cdk::api::msg_caller();
    let from_account = arg.from_account.unwrap_or_else(|| Account {
        owner: caller,
        subaccount: None,
    });
    if from_account.owner != caller {
        return Err(GetAllowancesError::AccessDenied {
            reason: "Caller is not the account owner".to_string(),
        });
    }
    // ... rest of function
}
```

This makes `AccessDenied` reachable and enforces that only the account owner (or a delegated canister) can enumerate their own allowances, which is the evident intent of the error variant's existence.

### Proof of Concept

1. Alice (`principal A`) calls `icrc2_approve` granting spender `principal S` an allowance of 1000 tokens.
2. Attacker (`principal X`, or anonymous) calls:
   ```
   icrc103_get_allowances({ from_account = opt { owner = A; subaccount = null }; prev_spender = null; take = null })
   ```
3. Response: `Ok([{ from_account = {A, null}; to_spender = {S, null}; allowance = 1000; expires_at = ... }])`
4. Attacker now knows Alice has approved `S` for 1000 tokens — without Alice's knowledge and without any authorization.

The existing test suite at `rs/ledger_suite/tests/sm-tests/src/allowances.rs` calls `list_allowances` always as the account owner, never testing a third-party caller, which is why this gap was not caught. [5](#0-4)

### Citations

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

**File:** packages/icrc-ledger-types/src/icrc103/get_allowances.rs (L16-19)
```rust
pub enum GetAllowancesError {
    AccessDenied { reason: String },
    GenericError { error_code: Nat, message: String },
}
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L542-550)
```text
type GetAllowancesError = variant {
  AccessDenied : record {
    reason : text
  };
  GenericError : record {
    error_code : nat;
    message : text
  }
};
```

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

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L3023-3025)
```rust
    let allowances = list_allowances(&env, canister_id, approver_none.owner, args.clone())
        .expect("failed to list allowances");
    assert_eq!(allowances.len(), 1);
```
