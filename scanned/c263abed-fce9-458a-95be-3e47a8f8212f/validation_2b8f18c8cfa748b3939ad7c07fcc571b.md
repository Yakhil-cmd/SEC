### Title
Incorrect Self-Approval Check Compares Only `owner` Principal Instead of Full `Account`, Blocking Legitimate Cross-Subaccount Approvals - (`rs/ledger_suite/icp/ledger/src/main.rs`, `rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

Both the ICP ledger and the ICRC-1 ledger `icrc2_approve` endpoints check for self-approval by comparing only the `owner` field (a `Principal`) of the `from_account` and the `spender`. Because ICRC-1 accounts are identified by `(owner, subaccount)` pairs, two accounts sharing the same `owner` but having different `subaccounts` are **distinct accounts**. The overly broad check incorrectly traps any approval where the caller's principal matches the spender's principal, even when the subaccounts differ. This is the direct IC analog of the Amphor `_transferTokenInAndApprove` bug: the wrong identity is used in a guard condition, causing legitimate operations to unexpectedly fail.

---

### Finding Description

In `rs/ledger_suite/icp/ledger/src/main.rs` inside `icrc2_approve_not_async`:

```rust
let from_account = Account {
    owner: caller,
    subaccount: arg.from_subaccount,
};
let from = AccountIdentifier::from(from_account);
if from_account.owner == arg.spender.owner {   // ← only owner compared
    trap("self approval is not allowed");
}
``` [1](#0-0) 

The identical pattern appears in `rs/ledger_suite/icrc1/ledger/src/main.rs`:

```rust
let from_account = Account {
    owner: caller,
    subaccount: arg.from_subaccount,
};
if from_account.owner == arg.spender.owner {   // ← only owner compared
    ic_cdk::trap("self approval is not allowed")
}
``` [2](#0-1) 

The correct check — used in the lower-level `AllowanceTable::approve` in `rs/ledger_suite/common/ledger_core/src/approvals.rs` — compares the **full account** (owner + subaccount):

```rust
if account == spender {
    return Err(ApproveError::SelfApproval);
}
``` [3](#0-2) 

The endpoint-level guard fires **before** the lower-level check is ever reached, so the lower-level correctness does not rescue the situation.

---

### Impact Explanation

Any unprivileged user whose principal owns more than one subaccount — a common pattern for DeFi protocols, canister-based wallets, and multi-purpose accounts — is permanently blocked from granting spending authority from one of their subaccounts to another. For example:

- `from_account = { owner: alice, subaccount: Some([1u8;32]) }`
- `spender     = { owner: alice, subaccount: Some([2u8;32]) }`

These are distinct ICRC-1 accounts. The approval is legitimate and useful (e.g., a canister acting as a router across its own subaccounts). The call traps with `"self approval is not allowed"`, permanently denying the operation with no workaround. Token flows that depend on cross-subaccount delegation are broken.

---

### Likelihood Explanation

The entry path requires only a standard `icrc2_approve` ingress call — no privilege, no key, no majority. Any user or canister that attempts cross-subaccount delegation triggers the bug deterministically. The pattern is common in DeFi integrations and canister-managed treasuries on the IC.

---

### Recommendation

Replace the principal-only comparison with a full `Account` equality check at the endpoint level, consistent with what the core library already does:

```rust
// ICP ledger (icrc2_approve_not_async)
- if from_account.owner == arg.spender.owner {
+ if from_account == arg.spender {
      trap("self approval is not allowed");
  }
```

```rust
// ICRC-1 ledger (icrc2_approve_not_async)
- if from_account.owner == arg.spender.owner {
+ if from_account == arg.spender {
      ic_cdk::trap("self approval is not allowed")
  }
```

This aligns the endpoint guard with the ICRC-2 specification (which prohibits approving the same account, not the same principal) and with the lower-level `AllowanceTable::approve` check.

---

### Proof of Concept

1. Alice holds tokens in subaccount `[1u8;32]` and wants to approve her own subaccount `[2u8;32]` as a spender.
2. Alice calls `icrc2_approve` with:
   - `from_subaccount = Some([1u8;32])`
   - `spender = { owner: alice, subaccount: Some([2u8;32]) }`
3. The endpoint evaluates `from_account.owner == arg.spender.owner` → `alice == alice` → `true`.
4. The canister traps: `"self approval is not allowed"`.
5. The approval never reaches `AllowanceTable::approve`; the transaction is permanently rejected despite being a valid cross-subaccount delegation between two distinct ICRC-1 accounts.

### Citations

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

**File:** rs/ledger_suite/common/ledger_core/src/approvals.rs (L243-245)
```rust
            if account == spender {
                return Err(ApproveError::SelfApproval);
            }
```
