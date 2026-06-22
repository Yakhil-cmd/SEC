### Title
Unrestricted Allowance Enumeration via `icrc103_get_allowances` — Missing Caller Authorization Check — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

### Summary

`icrc103_get_allowances` accepts an arbitrary `from_account` from any caller with no check that the caller is the account owner. The `AccessDenied` error variant is defined in the type system but is structurally unreachable in the implementation. Any unprivileged principal can enumerate the complete set of spender relationships and allowance amounts for any account on the ledger.

---

### Finding Description

The handler at line 1215 of `rs/ledger_suite/icrc1/ledger/src/main.rs` is:

```rust
#[query]
fn icrc103_get_allowances(arg: GetAllowancesArgs) -> Result<Allowances, GetAllowancesError> {
    let from_account = arg.from_account.unwrap_or_else(|| Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: None,
    });
    // ... no caller == from_account.owner check ...
    Ok(get_allowances(from_account, arg.prev_spender, max_results, ic_cdk::api::time()))
}
``` [1](#0-0) 

When `from_account` is `None`, it defaults to the caller's own account. When `from_account` is `Some(victim_account)`, the implementation uses it directly — **no check is performed that `msg_caller() == from_account.owner`**.

The `GetAllowancesError` type explicitly defines an `AccessDenied` variant:

```rust
pub enum GetAllowancesError {
    AccessDenied { reason: String },
    GenericError { error_code: Nat, message: String },
}
``` [2](#0-1) 

This variant is **never constructed or returned** anywhere in the production implementation. The function body always returns `Ok(...)`. [3](#0-2) 

The endpoint is exposed as a public `query` in the Candid interface: [4](#0-3) 

---

### Impact Explanation

An unprivileged attacker can call `icrc103_get_allowances` with `from_account = Some({ owner: victim_principal, subaccount: None })` and receive the complete paginated list of every spender the victim has approved, along with exact allowance amounts and expiry times. This is qualitatively different from `icrc2_allowance` (ICRC-2), which requires the querier to already know the specific spender — `icrc103_get_allowances` enables **discovery** of all spenders without prior knowledge.

The concrete impact is:
- Full enumeration of a victim's DeFi relationships (which DEXes, protocols, or principals they have approved)
- Exact allowance amounts and expiry timestamps for each relationship
- Potential targeting of accounts with large outstanding allowances for social engineering or phishing

---

### Likelihood Explanation

The exploit path requires zero privileges: any principal (including the anonymous principal) can issue a query call to the ledger canister. The call is free (query), requires no cycles, and is immediately effective. The `AccessDenied` guard is structurally absent — there is no code path that could return it.

---

### Recommendation

Add a caller authorization check before returning results when `from_account` is explicitly specified:

```rust
fn icrc103_get_allowances(arg: GetAllowancesArgs) -> Result<Allowances, GetAllowancesError> {
    let caller = ic_cdk::api::msg_caller();
    let from_account = arg.from_account.unwrap_or_else(|| Account {
        owner: caller,
        subaccount: None,
    });
    if from_account.owner != caller {
        return Err(GetAllowancesError::AccessDenied {
            reason: "Caller is not the owner of the from_account".to_string(),
        });
    }
    // ... rest of implementation
}
```

This matches the intent signaled by the `AccessDenied` variant in the ICRC-103 type definition.

---

### Proof of Concept

```rust
// Pseudocode unit test
let victim = principal_from_str("victim-principal-id");
let attacker = principal_from_str("attacker-principal-id");

// Victim creates approvals
ledger.icrc2_approve(caller=victim, spender=dex1, amount=1000);
ledger.icrc2_approve(caller=victim, spender=dex2, amount=500);

// Attacker enumerates victim's allowances — no authorization required
let result = ledger.icrc103_get_allowances(
    caller=attacker,
    args=GetAllowancesArgs {
        from_account: Some(Account { owner: victim, subaccount: None }),
        prev_spender: None,
        take: None,
    }
);

// result == Ok([{from: victim, to: dex1, allowance: 1000}, {from: victim, to: dex2, allowance: 500}])
// AccessDenied is never returned — the Ok branch is always taken
assert!(result.is_ok());
assert_eq!(result.unwrap().len(), 2);
```

The `AccessDenied` arm in `GetAllowancesError` is confirmed unreachable by inspection: the only `return` in `icrc103_get_allowances` is `Ok(get_allowances(...))`. [3](#0-2)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1215-1232)
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
}
```

**File:** packages/icrc-ledger-types/src/icrc103/get_allowances.rs (L16-19)
```rust
pub enum GetAllowancesError {
    AccessDenied { reason: String },
    GenericError { error_code: Nat, message: String },
}
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L634-634)
```text
  icrc103_get_allowances : (GetAllowancesArgs) -> (icrc103_get_allowances_response) query;
```
