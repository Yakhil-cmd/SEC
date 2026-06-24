### Title
ICRC-2 Self-Approval Check Diverges from Specification: Owner-Only Comparison Incorrectly Rejects Valid Cross-Subaccount Approvals - (File: `rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The `icrc2_approve_not_async` function in both the ICRC-1 ledger and the ICP ledger enforces the self-approval prohibition by comparing only the `owner` principal field, rather than the full `Account` identity (owner + subaccount). This diverges from the ICRC-2 specification, which defines accounts as `(owner, subaccount)` pairs and only prohibits self-approval when the complete account is identical. As a result, legitimate cross-subaccount approvals — explicitly permitted by the ICRC-2 standard — are incorrectly rejected with a trap.

---

### Finding Description

The ICRC-2 standard defines an account as a composite of `(owner: principal, subaccount: opt blob)`. Two accounts are distinct if either field differs. The self-approval prohibition applies only when the spender account is **identical** to the from-account (same owner **and** same subaccount).

In `icrc2_approve_not_async` in the ICRC-1 ledger:

```rust
// rs/ledger_suite/icrc1/ledger/src/main.rs, line 828
if from_account.owner == arg.spender.owner {
    ic_cdk::trap("self approval is not allowed")
}
```

This check compares only the `owner` principal. Consequently, a caller with `{owner: Alice, subaccount: None}` cannot approve `{owner: Alice, subaccount: Some([1u8;32])}` as a spender, even though these are **distinct accounts** under the ICRC-2 account model.

The identical overly-restrictive check exists in the ICP ledger:

```rust
// rs/ledger_suite/icp/ledger/src/main.rs, line 1328
if from_account.owner == arg.spender.owner {
    trap("self approval is not allowed");
}
```

The divergence is made explicit by comparing with the underlying `AllowanceTable::approve` in the shared ledger core, which correctly uses full account equality:

```rust
// rs/ledger_suite/common/ledger_core/src/approvals.rs, line 243
if account == spender {
    return Err(ApproveError::SelfApproval);
}
```

This correct inner check is **never reached** for cross-subaccount cases because the outer trap fires first. The two layers implement different semantics for the same rule, with the outer layer being more restrictive than the specification requires.

---

### Impact Explanation

Any user or canister that attempts a cross-subaccount approval — e.g., approving `{owner: Alice, subaccount: Some([1;32])}` to spend from `{owner: Alice, subaccount: None}` — receives an unexpected trap. This is a valid ICRC-2 use case (e.g., a user delegating spending authority from their main account to a dedicated trading subaccount). Protocols and canisters built against the ICRC-2 specification that rely on this pattern will fail silently or trap, potentially locking funds or breaking workflows. The impact is a persistent, deterministic specification divergence affecting all ICRC-1 and ICP ledger deployments.

---

### Likelihood Explanation

The entry path is a direct, unprivileged ingress call to `icrc2_approve`. No special role or key is required. Any user who holds multiple subaccounts under the same principal and attempts a cross-subaccount approval will trigger this. The use case is realistic and explicitly supported by the ICRC-2 standard's account model.

---

### Recommendation

Replace the owner-only comparison with a full account comparison in both ledgers:

```rust
// ICRC-1 ledger fix (rs/ledger_suite/icrc1/ledger/src/main.rs)
if from_account == arg.spender {
    ic_cdk::trap("self approval is not allowed")
}

// ICP ledger fix (rs/ledger_suite/icp/ledger/src/main.rs)
if from_account == Account::from(arg.spender) {
    trap("self approval is not allowed");
}
```

This aligns the outer guard with the inner `AllowanceTable::approve` check and with the ICRC-2 specification.

---

### Proof of Concept

1. Alice holds two accounts: `A1 = {owner: Alice, subaccount: None}` and `A2 = {owner: Alice, subaccount: Some([1u8;32])}`.
2. Alice calls `icrc2_approve` as `A1` with `spender = A2` and `amount = 1_000_000`.
3. The ledger traps with `"self approval is not allowed"` at line 828 of `rs/ledger_suite/icrc1/ledger/src/main.rs`, because `from_account.owner == arg.spender.owner` evaluates to `true`.
4. Per the ICRC-2 specification, `A1` and `A2` are distinct accounts; the approval must succeed.
5. The same trap occurs in the ICP ledger at line 1328 of `rs/ledger_suite/icp/ledger/src/main.rs`.
6. The underlying `AllowanceTable::approve` at line 243 of `rs/ledger_suite/common/ledger_core/src/approvals.rs` would correctly permit this approval if reached, confirming the divergence is in the outer guard only. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L824-830)
```rust
        let from_account = Account {
            owner: caller,
            subaccount: arg.from_subaccount,
        };
        if from_account.owner == arg.spender.owner {
            ic_cdk::trap("self approval is not allowed")
        }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1323-1330)
```rust
    let from_account = Account {
        owner: caller,
        subaccount: arg.from_subaccount,
    };
    let from = AccountIdentifier::from(from_account);
    if from_account.owner == arg.spender.owner {
        trap("self approval is not allowed");
    }
```

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L242-245)
```rust
        self.with_postconditions_check(|table| {
            if account == spender {
                return Err(ApproveError::SelfApproval);
            }
```
