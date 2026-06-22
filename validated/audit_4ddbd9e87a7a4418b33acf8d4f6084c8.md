### Title
Incorrect Access Control in `icrc152_burn`: Controller Can Drain Any User's Account Without Consent - (File: rs/ledger_suite/icrc1/ledger/src/main.rs)

### Summary
The `icrc152_burn` endpoint in the ICRC-1 ledger canister allows any controller of the ledger to burn tokens from **any arbitrary account** (`args.from`) without requiring consent from the account owner. The only access check is `is_controller(&caller)`, but there is no verification that the caller has authorization from the `args.from` account holder. This is a direct analog to the CosmWasm incorrect access control vulnerability: a malicious canister that is a controller of an ICRC-152-enabled ledger can unilaterally destroy any user's token balance.

### Finding Description
In `icrc152_burn_not_async`, the authorization check is:

```rust
if !ic_cdk::api::is_controller(&caller) {
    return Err(Icrc152BurnError::Unauthorized(
        "caller is not a controller".to_string(),
    ));
}
```

After passing this check, the function proceeds to apply an `AuthorizedBurn` transaction using the caller-supplied `args.from` account:

```rust
let tx = Transaction {
    operation: Operation::AuthorizedBurn {
        from: args.from,   // ← attacker-controlled, any account
        amount,
        caller: Some(caller),
        ...
    },
    ...
};
let (block_idx, _) = apply_transaction(ledger, tx, now, Tokens::zero())...
```

There is no check that `args.from.owner == caller` or that the `args.from` account owner has granted any approval. The controller can supply any principal as `args.from` and the ledger will deduct the balance.

The same pattern exists in `icrc152_mint_not_async` (lines 905–988), where a controller can mint tokens to any `args.to` account, inflating supply without governance approval.

### Impact Explanation
Any canister that is a controller of an ICRC-152-enabled ledger can:
1. Call `icrc152_burn` with `from` set to any user's account.
2. Destroy that user's entire token balance without their knowledge or consent.
3. Repeat for all accounts, draining the entire ledger.

This constitutes a direct, irreversible **loss of funds** for all token holders on any ICRC-152-enabled ledger whose controller is malicious or compromised. The `icrc152_mint` variant allows unbounded token inflation, diluting all existing holders.

### Likelihood Explanation
The ICRC-152 feature is opt-in (gated by `ledger.feature_flags().icrc152`). Any developer who deploys their own ICRC-152-enabled ledger is automatically a controller and can exploit this immediately. Users who hold tokens on third-party ICRC-152 ledgers are exposed to this risk. The attack requires no special network access, no threshold corruption, and no governance majority — only controller status on the ledger canister, which is the normal state for any canister deployer.

### Recommendation
Add an ownership/consent check in `icrc152_burn_not_async` to ensure the caller is only permitted to burn from accounts they own or have been explicitly approved to burn from. At minimum:

```rust
if args.from.owner != caller {
    return Err(Icrc152BurnError::Unauthorized(
        "caller can only burn from their own account".to_string(),
    ));
}
```

Alternatively, integrate with the ICRC-2 approval mechanism so that burning from a third-party account requires a prior `icrc2_approve` from the account owner.

### Proof of Concept

1. Deploy an ICRC-1 ledger with `icrc152: true` in `InitArgs`. The deployer is automatically a controller.
2. A victim user calls `icrc1_transfer` to deposit tokens into their account on this ledger.
3. The controller (attacker) calls `icrc152_burn` with:
   ```
   Icrc152BurnArgs {
       from: Account { owner: victim_principal, subaccount: None },
       amount: victim_balance,
       created_at_time: current_time,
       reason: None,
   }
   ```
4. The ledger executes `icrc152_burn_not_async`, passes the `is_controller` check, and applies `Operation::AuthorizedBurn` against the victim's account.
5. The victim's balance is reduced to zero with no consent or prior approval required.

The attacker-controlled entry path is a direct ingress call to the `icrc152_burn` update method, reachable by any canister controller. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L916-920)
```rust
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152MintError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L990-996)
```rust
#[update]
async fn icrc152_mint(args: Icrc152MintArgs) -> Result<Nat, Icrc152MintError> {
    let block_idx = icrc152_mint_not_async(ic_cdk::api::msg_caller(), args)?;
    ic_cdk::api::certified_data_set(Access::with_ledger(Ledger::root_hash));
    archive_blocks::<Access>(&LOG, MAX_MESSAGE_SIZE).await;
    Ok(Nat::from(block_idx))
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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1044-1051)
```rust
        let tx = Transaction {
            operation: Operation::AuthorizedBurn {
                from: args.from,
                amount,
                caller: Some(caller),
                mthd: Some(MTHD_152_BURN.to_string()),
                reason: args.reason,
            },
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1088-1094)
```rust
#[update]
async fn icrc152_burn(args: Icrc152BurnArgs) -> Result<Nat, Icrc152BurnError> {
    let block_idx = icrc152_burn_not_async(ic_cdk::api::msg_caller(), args)?;
    ic_cdk::api::certified_data_set(Access::with_ledger(Ledger::root_hash));
    archive_blocks::<Access>(&LOG, MAX_MESSAGE_SIZE).await;
    Ok(Nat::from(block_idx))
}
```
