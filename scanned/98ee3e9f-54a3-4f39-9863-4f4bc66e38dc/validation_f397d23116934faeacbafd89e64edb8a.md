Based on my direct code inspection, this is a real finding.

---

### Title
Unauthenticated Allowance Enumeration via `icrc103_get_allowances` — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

### Summary
Any unprivileged caller can enumerate the full allowance set of any arbitrary account by supplying an explicit `from_account` argument. The `AccessDenied` error variant is defined in the type system but is **dead code** — it is never returned by the implementation.

### Finding Description

The handler at line 1215 of `rs/ledger_suite/icrc1/ledger/src/main.rs` performs no caller-identity check when `from_account` is explicitly supplied: [1](#0-0) 

The only "default-to-caller" logic applies when `from_account` is `None`: [2](#0-1) 

When `from_account` is `Some(victim_account)`, the supplied account is used verbatim and the function unconditionally returns `Ok(...)`. The `AccessDenied` variant defined in the error type: [3](#0-2) 

is never constructed or returned anywhere in the production implementation. The entire `Result` error path for authorization is unreachable.

### Impact Explanation
An attacker can call `icrc103_get_allowances` with `from_account = Some({owner: victim_principal, subaccount: None})` as any arbitrary principal and receive the complete list of `(spender, allowance_amount, expiry)` tuples for the victim. This reveals:
- Which principals the victim has approved to spend on their behalf
- The exact approved amounts and expiry timestamps

This is a **privacy/information-disclosure** vulnerability. It does not directly enable fund theft, but it exposes the full approval graph of any account to any observer.

### Likelihood Explanation
Exploitation requires only a standard query call — no privileged access, no key material, no governance majority. It is trivially reachable from any IC ingress client. The `#[query]` annotation means it is also free (no cycles cost to the attacker).

### Recommendation
Before calling `get_allowances`, check that `ic_cdk::api::msg_caller() == from_account.owner`. If not, return `Err(GetAllowancesError::AccessDenied { reason: "caller is not the account owner".to_string() })`. This makes the existing `AccessDenied` variant reachable and enforces the authorization invariant the type system already models.

### Proof of Concept
```rust
// attacker_principal calls:
icrc103_get_allowances(GetAllowancesArgs {
    from_account: Some(Account { owner: victim_principal, subaccount: None }),
    prev_spender: None,
    take: None,
})
// Returns Ok([...all of victim's approvals...])
// AccessDenied is never reached.
```

The test helper in `rs/ledger_suite/tests/sm-tests/src/allowances.rs` already demonstrates calling the endpoint as an arbitrary `from` principal: [4](#0-3) 

A unit test where `from != caller` and the call succeeds would confirm the missing guard.

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

**File:** packages/icrc-ledger-types/src/icrc103/get_allowances.rs (L16-19)
```rust
pub enum GetAllowancesError {
    AccessDenied { reason: String },
    GenericError { error_code: Nat, message: String },
}
```

**File:** rs/ledger_suite/tests/sm-tests/src/allowances.rs (L8-26)
```rust
pub fn list_allowances(
    env: &StateMachine,
    ledger: CanisterId,
    from: Principal,
    args: GetAllowancesArgs,
) -> Result<Allowances, GetAllowancesError> {
    Decode!(
        &env.execute_ingress_as(
            PrincipalId(from),
            ledger,
            "icrc103_get_allowances",
            Encode!(&args)
            .unwrap()
        )
        .expect("failed to list allowances")
        .bytes(),
        Result<Allowances, GetAllowancesError>
    )
    .expect("failed to decode icrc103_get_allowances response")
```
