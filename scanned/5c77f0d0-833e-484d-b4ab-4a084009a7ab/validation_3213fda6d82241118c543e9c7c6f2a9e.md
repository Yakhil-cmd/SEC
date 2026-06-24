### Title
`icrc2_transfer_from` Does Not Check `can_send` for the `from` Account, Allowing Restricted Accounts' Tokens to Be Drained via Pre-Existing Allowances - (`rs/ledger_suite/icp/ledger/src/main.rs`)

---

### Summary

The ICP ledger's `icrc2_transfer_from` endpoint enforces the `can_send` restriction only on the **caller** (the spender), but never on the **`from` account** (the token owner whose funds are being moved). If a `from` account is restricted after having previously granted an allowance, a spender with a pre-existing allowance can still drain the restricted account's tokens.

---

### Finding Description

In `rs/ledger_suite/icp/ledger/src/main.rs`, the `icrc2_transfer_from` handler performs a `can_send` check exclusively on `caller()` (the spender): [1](#0-0) 

The `from` account is taken directly from `arg.from` (caller-supplied) and is passed into `icrc1_send` without any `can_send` validation: [2](#0-1) 

By contrast, `icrc2_approve` does check `can_send` for the caller (the account owner granting the approval): [3](#0-2) 

This creates an inconsistency: the restriction is enforced at approval time for the `from` account, but not at spend time. A `from` account that becomes restricted after granting an allowance is not protected from having its tokens moved by a spender.

The same asymmetry exists in `icrc1_transfer`, which checks `can_send` for the caller (who is also the `from` account), so that path is consistent. The gap is specific to the delegated-spend path (`icrc2_transfer_from`). [4](#0-3) 

---

### Impact Explanation

An unprivileged spender (any ingress sender) who holds a pre-existing allowance from a `from` account that has since become restricted can call `icrc2_transfer_from` and successfully move the restricted account's tokens to an arbitrary destination. The restriction mechanism intended to freeze or block the `from` account is bypassed entirely for the delegated-spend path. This is a **ledger conservation / authorization bypass** bug: the `can_send` invariant is violated for the `from` account.

---

### Likelihood Explanation

The scenario is realistic and requires no privileged access:
1. Account A grants an allowance to account B via `icrc2_approve` (A passes `can_send` at this point).
2. A later becomes restricted (`can_send` returns `false` for A's principal).
3. B calls `icrc2_transfer_from(from=A, to=C, amount=X)` as an ordinary ingress message.
4. Only B's `can_send` is checked; A's restriction is never evaluated.
5. The transfer succeeds, draining A's tokens.

The attacker-controlled entry path is a standard `icrc2_transfer_from` ingress call — no admin key, no governance majority, no threshold attack required.

---

### Recommendation

Add a `can_send` check for `arg.from.owner` inside `icrc2_transfer_from`, mirroring the check already applied to the caller:

```rust
if !LEDGER.read().unwrap().can_send(&PrincipalId::from(arg.from.owner)) {
    trap("The from account cannot hold tokens on the ledger.");
}
```

This should be placed immediately after the existing caller check, before `icrc1_send` is invoked. [5](#0-4) 

---

### Proof of Concept

```
1. Principal A calls icrc2_approve(spender=B, amount=1_000_000) → allowance recorded.
2. Ledger admin restricts A (can_send(A) now returns false).
3. Principal B calls icrc2_transfer_from(from=A, to=C, amount=1_000_000).
4. Ledger checks can_send(B) → true (B is not restricted).
5. No check is performed on can_send(A).
6. icrc1_send executes, moving 1_000_000 tokens out of A's account.
7. A's restriction is bypassed; tokens are drained.
```

The root cause is the missing `can_send` guard for `arg.from.owner` in `icrc2_transfer_from` at `rs/ledger_suite/icp/ledger/src/main.rs` lines 845–882. [5](#0-4)

### Citations

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L807-817)
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

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L1313-1316)
```rust
) -> Result<Nat, ApproveError> {
    if !LEDGER.read().unwrap().can_send(&PrincipalId::from(caller)) {
        trap("Caller cannot approve token transfers on the ledger.");
    }
```
