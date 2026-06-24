### Title
Controller Cannot Burn Tokens from Anonymous-Principal Account via `icrc152_burn` - (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

### Summary

The `icrc152_burn` endpoint is the ICRC-152 privileged compliance/regulatory burn operation, callable only by a canister controller. It is designed to allow the controller to forcibly burn tokens from **any** account. However, a hard-coded `InvalidAccount` guard on the `from` principal blocks the controller from burning tokens held in the anonymous-principal account, even though tokens can legitimately accumulate there via the unrestricted `icrc1_transfer` endpoint.

### Finding Description

`icrc152_burn_not_async` in `rs/ledger_suite/icrc1/ledger/src/main.rs` performs the following check before constructing the `AuthorizedBurn` transaction:

```rust
if args.from.owner == Principal::anonymous() {
    return Err(Icrc152BurnError::InvalidAccount(
        "anonymous principal is not allowed".to_string(),
    ));
}
``` [1](#0-0) 

This guard fires unconditionally, regardless of whether the caller is a verified controller. The `icrc1_transfer` endpoint in the same ledger places no restriction on the `to` field being the anonymous principal:

```rust
pub async fn icrc1_transfer(arg: TransferArg) -> Result<Nat, TransferError> {
    let from_account = Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: arg.from_subaccount,
    };
    execute_transfer(from_account, arg.to, ...)
``` [2](#0-1) 

Consequently, any user can transfer tokens into `Account { owner: Principal::anonymous(), subaccount: None }`. Once tokens reside there, the controller — the only entity authorized to call `icrc152_burn` — is permanently blocked from burning them by the `InvalidAccount` guard. The `AuthorizedBurn` operation at the core layer itself has no such restriction; it simply calls `context.balances_mut().burn(from, amount)`: [3](#0-2) 

The controller authorization check is correct: [4](#0-3) 

But the subsequent `from.owner == Principal::anonymous()` guard does not exempt the controller, mirroring exactly the pattern in the referenced Solidity report where `_beforeTokenTransfer` checked KYC status without exempting `BURNER_ROLE`.

### Impact Explanation

A controller deploying an ICRC-152-enabled ledger for compliance or regulatory purposes (e.g., to freeze or confiscate tokens) cannot burn tokens that have been sent to the anonymous-principal account. Those tokens are permanently unburnable via the privileged path, breaking the invariant that `icrc152_burn` can reach any non-minting account. Total supply cannot be corrected for those tokens, and any compliance obligation tied to burning them cannot be fulfilled.

### Likelihood Explanation

Any unprivileged ingress sender can call `icrc1_transfer` with `to = Account { owner: Principal::anonymous(), subaccount: None }` and deposit tokens into the anonymous account. No special role or access is required. Once deposited, the controller's `icrc152_burn` call will always return `Icrc152BurnError::InvalidAccount`, with no alternative privileged path to remove those tokens.

### Recommendation

Remove the anonymous-principal guard from `icrc152_burn_not_async`, or scope it so that a verified controller is exempt:

```rust
// Before (blocks controller):
if args.from.owner == Principal::anonymous() {
    return Err(Icrc152BurnError::InvalidAccount(...));
}

// After (controller may burn from any account):
if args.from.owner == Principal::anonymous()
    && !ic_cdk::api::is_controller(&caller)
{
    return Err(Icrc152BurnError::InvalidAccount(...));
}
```

The analogous fix in the referenced Solidity report was to add `&& !hasRole(BURNER_ROLE, msg.sender)` to the `from != address(0)` guard in `_beforeTokenTransfer`.

### Proof of Concept

1. Deploy an ICRC-1 ledger with `icrc152: true` and a known controller.
2. As any user, call `icrc1_transfer` with `to = { owner = principal "2vxsx-fae"; subaccount = null }` (the anonymous principal) to deposit tokens.
3. Confirm balance: `icrc1_balance_of({ owner = principal "2vxsx-fae"; subaccount = null })` returns a non-zero value.
4. As the controller, call `icrc152_burn({ from = { owner = principal "2vxsx-fae"; subaccount = null }; amount = <deposited amount>; created_at_time = <now>; reason = null })`.
5. Observe: the call returns `Err(InvalidAccount("anonymous principal is not allowed"))` instead of succeeding.

The tokens remain permanently locked in the anonymous account, unreachable by the privileged burn path. [5](#0-4)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L675-699)
```rust
#[update]
async fn icrc1_transfer(arg: TransferArg) -> Result<Nat, TransferError> {
    let from_account = Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: arg.from_subaccount,
    };
    execute_transfer(
        from_account,
        arg.to,
        None,
        arg.fee,
        arg.amount,
        arg.memo,
        arg.created_at_time,
    )
    .await
    .map_err(convert_transfer_error)
    .map_err(|err| {
        let err: TransferError = match err.try_into() {
            Ok(err) => err,
            Err(err) => ic_cdk::trap(&err),
        };
        err
    })
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1009-1013)
```rust
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152BurnError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1025-1034)
```rust
        if args.from.owner == Principal::anonymous() {
            return Err(Icrc152BurnError::InvalidAccount(
                "anonymous principal is not allowed".to_string(),
            ));
        }
        if &args.from == ledger.minting_account() {
            return Err(Icrc152BurnError::InvalidAccount(
                "cannot burn from the minting account".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/src/lib.rs (L562-564)
```rust
            Operation::AuthorizedBurn { from, amount, .. } => {
                context.balances_mut().burn(from, amount.clone())?;
            }
```
