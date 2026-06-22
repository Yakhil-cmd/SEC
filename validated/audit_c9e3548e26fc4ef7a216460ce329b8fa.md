The code at line 828 is confirmed: [1](#0-0) 

```rust
let from_account = Account {
    owner: caller,
    subaccount: arg.from_subaccount,
};
if from_account.owner == arg.spender.owner {
    ic_cdk::trap("self approval is not allowed")
}
```

The check compares only `.owner` (the principal), ignoring `.subaccount`. The ICRC-2 spec prohibits self-approval only when `from_account == spender` as a full account pair (owner + subaccount). The correct guard should be `from_account == arg.spender`.

---

### Title
Overly Broad Self-Approval Check Blocks Legitimate Cross-Subaccount ICRC-2 Approvals — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

### Summary
The self-approval guard in `icrc2_approve_not_async` compares only the principal (`owner`) of the `from` account and the `spender`, ignoring subaccounts. This causes any approval where `from.owner == spender.owner` — even with distinct subaccounts — to unconditionally trap, violating the ICRC-2 specification and permanently blocking a legitimate use case.

### Finding Description
In `icrc2_approve_not_async`, the guard is:

```rust
if from_account.owner == arg.spender.owner {
    ic_cdk::trap("self approval is not allowed")
}
``` [2](#0-1) 

The ICRC-2 standard defines an *account* as the pair `(owner: Principal, subaccount: Option<Subaccount>)`. Two accounts with the same principal but different subaccounts are distinct accounts. The spec only prohibits `from == spender` as a full account equality check. The correct guard is:

```rust
if from_account == arg.spender {
    ic_cdk::trap("self approval is not allowed")
}
```

### Impact Explanation
Any unprivileged user holding principal `P` with two distinct subaccounts `sub_A` and `sub_B` is permanently unable to create an ICRC-2 allowance from `(P, sub_A)` to `(P, sub_B)`. This is a valid and useful pattern (e.g., a canister managing multiple subaccounts delegating spend rights between them). The call traps unconditionally with no workaround at the protocol level.

Impact: **constrained availability** — a class of legitimate ICRC-2 operations is permanently rejected.

### Likelihood Explanation
Any user who controls a principal with multiple subaccounts and attempts cross-subaccount delegation hits this immediately. No special privileges or conditions are required. The entrypoint is the public `icrc2_approve` update call.

### Recommendation
Change the comparison from owner-only to full account equality:

```rust
if from_account == arg.spender {
    ic_cdk::trap("self approval is not allowed")
}
```

`Account` implements `PartialEq` comparing both `owner` and `subaccount`, so this is a one-line fix.

### Proof of Concept
```
caller = P (any principal)
arg.from_subaccount = Some([1u8; 32])   // sub_A
arg.spender = Account { owner: P, subaccount: Some([2u8; 32]) }  // sub_B

→ from_account.owner == arg.spender.owner  // true (both P)
→ ic_cdk::trap("self approval is not allowed")  // fires incorrectly
```

Per ICRC-2, `(P, sub_A) ≠ (P, sub_B)`, so this approval must be permitted. The trap is incorrect.

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
