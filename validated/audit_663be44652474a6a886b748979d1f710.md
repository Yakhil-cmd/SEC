### Title
Unauthenticated Allowance Enumeration via `icrc103_get_allowances` — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

`icrc103_get_allowances` accepts a caller-supplied `from_account` and returns all allowances for that account with **no authorization check**. Any unprivileged principal can enumerate the complete allowance state (spenders, amounts, expiry timestamps) of any other principal on the ledger. The `GetAllowancesError::AccessDenied` variant is defined but never emitted.

---

### Finding Description

The handler at lines 1214–1232 of `rs/ledger_suite/icrc1/ledger/src/main.rs`:

```rust
#[query]
fn icrc103_get_allowances(arg: GetAllowancesArgs) -> Result<Allowances, GetAllowancesError> {
    let from_account = arg.from_account.unwrap_or_else(|| Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: None,
    });
    // ... compute max_results ...
    Ok(get_allowances(
        from_account,
        arg.prev_spender,
        max_results,
        ic_cdk::api::time(),
    ))
}
```

When `from_account` is `None` the code correctly defaults to the caller's own account. When `from_account` is `Some(arbitrary_account)` the supplied account is used **verbatim** — there is no comparison of `from_account.owner` against `ic_cdk::api::msg_caller()`, no controller check, and no other guard. The function unconditionally returns `Ok(...)`.

The `GetAllowancesError::AccessDenied` variant is declared in the type definition but is **never constructed or returned** anywhere in the codebase. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

An unprivileged caller obtains, for any target principal:

- The identity of every spender that holds an active allowance from that principal
- The exact allowance amount for each spender
- The expiration timestamp of each allowance

This enables:
1. **Targeted front-running**: knowing that spender S holds allowance A from victim V, an attacker can watch the mempool and race `icrc2_transfer_from` calls.
2. **Financial surveillance**: full mapping of delegation relationships across the ledger without the account owner's knowledge or consent. [3](#0-2) 

---

### Likelihood Explanation

The attack requires only a standard query call — no tokens, no fees, no privileged role. It is reachable from any ingress query to the ledger canister. The `from_account` field is a plain optional argument with no validation. Exploitation is trivially scriptable. [4](#0-3) 

---

### Recommendation

Add a caller-identity guard immediately after resolving `from_account`:

```rust
let caller = ic_cdk::api::msg_caller();
let from_account = arg.from_account.unwrap_or_else(|| Account {
    owner: caller,
    subaccount: None,
});
if from_account.owner != caller {
    return Err(GetAllowancesError::AccessDenied {
        reason: "caller is not the account owner".to_string(),
    });
}
```

Controllers or a configurable allowlist could be exempted if operator-level inspection is a desired feature, but that must be an explicit, audited policy — not the current implicit open access. [5](#0-4) 

---

### Proof of Concept

State-machine test outline (two principals, no special privileges):

```rust
// 1. Principal A approves spenders S1, S2, S3 via icrc2_approve.
// 2. Principal B (unrelated) calls:
//    icrc103_get_allowances({
//        from_account: Some({ owner: principal_A, subaccount: None }),
//        prev_spender: None,
//        take: None,
//    })
// 3. Assert: result is Ok(allowances) where allowances.len() == 3.
// 4. Assert: GetAllowancesError::AccessDenied is never returned.
// Confirmed by inspection: the Ok(...) path is unconditional.
```

The existing test file at `rs/ledger_suite/tests/sm-tests/src/allowances.rs` already exercises `icrc103_get_allowances` and can be extended with a cross-principal call to confirm the absence of any access denial. [6](#0-5)

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

**File:** packages/icrc-ledger-types/src/icrc103/get_allowances.rs (L15-19)
```rust
#[derive(CandidType, Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub enum GetAllowancesError {
    AccessDenied { reason: String },
    GenericError { error_code: Nat, message: String },
}
```

**File:** packages/icrc-ledger-types/src/icrc103/get_allowances.rs (L22-31)
```rust
#[derive(CandidType, Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct Allowance {
    pub from_account: Account,
    pub to_spender: Account,
    pub allowance: Nat,
    pub expires_at: Option<u64>,
}

/// The allowances vector returned by the `icrc103_get_allowances` endpoint.
pub type Allowances = Vec<Allowance>;
```

**File:** rs/ledger_suite/tests/sm-tests/src/allowances.rs (L1-2)
```rust
use candid::{Decode, Encode, Principal};
use ic_base_types::{CanisterId, PrincipalId};
```
