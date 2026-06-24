### Title
`icrc2_transfer_from` Checks `can_send` on Caller (Spender) Instead of `from` Account Owner, Bypassing Transfer Restriction - (File: `rs/ledger_suite/icp/ledger/src/main.rs`)

---

### Summary

The ICP ledger's `icrc2_transfer_from` endpoint enforces a `can_send` restriction by checking the **caller** (the spender/operator), not the **`from`** account (the actual token owner). This is the direct IC analog of the reported EVM bug: a single whitelisted spender can move tokens out of any non-whitelisted `from` account that has previously granted it an allowance, completely bypassing the `can_send` guard for the token owner.

---

### Finding Description

In `rs/ledger_suite/icp/ledger/src/main.rs`, the `icrc2_transfer_from` handler performs the following check before executing the transfer:

```rust
#[update]
async fn icrc2_transfer_from(arg: TransferFromArgs) -> Result<Nat, TransferFromError> {
    if !LEDGER
        .read()
        .unwrap()
        .can_send(&PrincipalId::from(caller()))   // ← checks spender, not arg.from
    {
        trap("Caller cannot hold tokens on the ledger.");
    }
    ...
    let spender_account = Account {
        owner: caller(),
        subaccount: arg.spender_subaccount,
    };
    Ok(Nat::from(
        icrc1_send(
            arg.memo,
            arg.amount,
            arg.fee,
            arg.from,          // ← tokens are debited from here
            arg.to,
            Some(spender_account),
            arg.created_at_time,
        )
        .await
        ...
    ))
}
``` [1](#0-0) 

The `can_send` predicate is evaluated against `caller()`, which is the **spender** (the operator acting on behalf of the token owner). The tokens, however, are debited from `arg.from`, which is a **separate, caller-supplied account**. The `from` account's owner is never checked against `can_send`.

By contrast, the `icrc1_transfer` handler correctly checks `can_send` on `caller()` because in that flow the caller **is** the `from` account owner — there is no delegation:

```rust
async fn icrc1_transfer(arg: TransferArg) -> ... {
    if !LEDGER.read().unwrap().can_send(&PrincipalId::from(caller())) {
        trap("Caller cannot hold tokens on the ledger.");
    }
    let from_account = Account { owner: caller(), ... };
    ...
}
``` [2](#0-1) 

In `icrc2_transfer_from` the caller and the `from` owner are different principals, so the check on `caller()` does not protect the `from` account.

---

### Impact Explanation

`can_send` is a ledger-level restriction that controls which principals are permitted to hold and transfer tokens. When this restriction is active, a principal that fails `can_send` should be unable to have its tokens moved. Because `icrc2_transfer_from` only validates the spender's eligibility, any non-whitelisted `from` account that has ever granted an allowance to a whitelisted spender (via `icrc2_approve`) can have its tokens drained by that spender. The `can_send` guard for the actual token owner is silently skipped.

Concretely:
- Non-whitelisted principal A approves whitelisted principal B for any amount.
- B calls `icrc2_transfer_from(from=A, to=B_or_anyone, amount=X)`.
- The `can_send(caller())` check passes because B is whitelisted.
- `arg.from` (A) is never checked; A's tokens are transferred despite A failing `can_send`.

This is a **ledger authorization bypass** — the `can_send` invariant is violated for the `from` account in every delegated transfer.

---

### Likelihood Explanation

The attack path is fully reachable by an unprivileged ingress sender:

1. Any principal can call `icrc2_approve` to grant an allowance to a whitelisted spender (the approve endpoint has no `can_send` guard on the approver in this context).
2. The whitelisted spender then calls `icrc2_transfer_from` as a normal update call.
3. No privileged access, no threshold corruption, no admin key is required.

The only precondition is that at least one whitelisted spender exists and that a non-whitelisted account has granted it an allowance — a realistic scenario whenever the ledger is deployed with `can_send` restrictions and ICRC-2 enabled.

---

### Recommendation

Change the `can_send` check in `icrc2_transfer_from` to validate the **`from` account owner** instead of (or in addition to) the caller:

```rust
// Check the token owner, not the spender
if !LEDGER
    .read()
    .unwrap()
    .can_send(&PrincipalId::from(arg.from.owner))
{
    trap("From account cannot hold tokens on the ledger.");
}
```

If the intent is to also restrict which spenders may act as operators, both checks should be present. The primary fix, mirroring the EVM report's recommendation, is to ensure the restriction is enforced on `from` (the account whose tokens are being moved).

---

### Proof of Concept

1. Deploy the ICP ledger with `can_send` restrictions active (a non-empty allowlist that excludes principal `A`).
2. Principal `A` (non-whitelisted) calls `icrc2_approve` granting principal `B` (whitelisted) a large allowance.
3. Principal `B` calls `icrc2_transfer_from` with `from = A's account`, `to = B`, `amount = X`.
4. The `can_send(caller())` check at line 850 passes because `B` is whitelisted.
5. `icrc1_send` is invoked with `arg.from = A`; `A`'s balance is debited.
6. `A`'s tokens are transferred despite `A` failing `can_send` — the restriction is bypassed. [3](#0-2)

### Citations

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L807-843)
```rust
#[update]
async fn icrc1_transfer(
    arg: TransferArg,
) -> Result<Nat, icrc_ledger_types::icrc1::transfer::TransferError> {
    if !LEDGER
        .read()
        .unwrap()
        .can_send(&PrincipalId::from(caller()))
    {
        trap("Caller cannot hold tokens on the ledger.");
    }

    let from_account = Account {
        owner: caller(),
        subaccount: arg.from_subaccount,
    };
    Ok(Nat::from(
        icrc1_send(
            arg.memo,
            arg.amount,
            arg.fee,
            from_account,
            arg.to,
            None,
            arg.created_at_time,
        )
        .await
        .map_err(convert_transfer_error)
        .map_err(|err| {
            let err: Icrc1TransferError = match Icrc1TransferError::try_from(err) {
                Ok(err) => err,
                Err(err) => trap(&err),
            };
            err
        })?,
    ))
}
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L845-882)
```rust
#[update]
async fn icrc2_transfer_from(arg: TransferFromArgs) -> Result<Nat, TransferFromError> {
    if !LEDGER
        .read()
        .unwrap()
        .can_send(&PrincipalId::from(caller()))
    {
        trap("Caller cannot hold tokens on the ledger.");
    }

    if !LEDGER.read().unwrap().feature_flags.icrc2 {
        trap("ICRC-2 features are not enabled on the ledger.");
    }
    let spender_account = Account {
        owner: caller(),
        subaccount: arg.spender_subaccount,
    };
    Ok(Nat::from(
        icrc1_send(
            arg.memo,
            arg.amount,
            arg.fee,
            arg.from,
            arg.to,
            Some(spender_account),
            arg.created_at_time,
        )
        .await
        .map_err(convert_transfer_error)
        .map_err(|err| {
            let err: TransferFromError = match TransferFromError::try_from(err) {
                Ok(err) => err,
                Err(err) => trap(&err),
            };
            err
        })?,
    ))
}
```
