### Title
ICRC-152 `icrc152_burn` Allows Any Canister Controller to Drain Arbitrary User Accounts Without Owner Consent - (File: `rs/ledger_suite/icrc1/ledger/src/main.rs`)

### Summary
The `icrc152_burn` endpoint in the ICRC-1 ledger canister allows any canister controller to burn tokens from **any** user account without that account owner's knowledge or consent. The caller-supplied `from` field is completely attacker-controlled, and the only authorization check is `is_controller(&caller)`. This is the direct IC analog of the `IFFixedSale.withdrawGiveaway()` vulnerability: a privileged-but-reachable role can specify an arbitrary target and drain it.

### Finding Description
In `rs/ledger_suite/icrc1/ledger/src/main.rs`, the `icrc152_burn_not_async` function (called by the public `#[update] icrc152_burn` endpoint) performs the following checks before executing a burn:

1. Feature flag `icrc152` is enabled.
2. `ic_cdk::api::is_controller(&caller)` — caller must be a controller of the ledger canister.
3. `args.amount != 0`.
4. `args.from.owner != Principal::anonymous()`.
5. `args.from != minting_account`.

There is **no check** that `args.from.owner == caller` or that the account owner has consented to the burn. The `from` account is entirely caller-supplied.

```rust
// rs/ledger_suite/icrc1/ledger/src/main.rs:998-1086
fn icrc152_burn_not_async(caller: Principal, args: Icrc152BurnArgs) -> Result<u64, Icrc152BurnError> {
    ...
    if !ic_cdk::api::is_controller(&caller) {
        return Err(Icrc152BurnError::Unauthorized(...));
    }
    // No check: args.from.owner == caller
    // No check: account owner approved this burn
    let tx = Transaction {
        operation: Operation::AuthorizedBurn {
            from: args.from,   // <-- fully attacker-controlled
            amount,
            ...
        },
        ...
    };
    apply_transaction(ledger, tx, now, Tokens::zero())...
``` [1](#0-0) [2](#0-1) 

The `icrc152_mint` function has the same structural pattern — a controller can mint to any account — but minting inflates supply rather than stealing existing balances, so the burn direction is the higher-severity path. [3](#0-2) 

### Impact Explanation
Any principal that is a controller of an ICRC-152-enabled ledger canister can call `icrc152_burn` with:
- `from`: any user's account (e.g., the account with the largest balance)
- `amount`: up to the full balance of that account

This results in permanent, irreversible destruction of the victim's tokens. The total supply is reduced and the victim's balance goes to zero. No approval, signature, or consent from the account owner is required. This is a **ledger conservation break** and **unauthorized asset destruction** affecting all holders of any token whose ledger has `icrc152: true` enabled. [4](#0-3) 

### Likelihood Explanation
The `icrc152` feature flag defaults to `false` in `FeatureFlags::const_default()`, so the attack surface is limited to ledgers that explicitly opt in. [5](#0-4) 

However, the feature is designed to be enabled (it has a full test suite, a DID interface, and is advertised via `icrc10_supported_standards`). Any SNS or custom token that enables it exposes all token holders to any current or future controller of that ledger canister. The ledger controller role is held by SNS Root and SNS-W for SNS-deployed ledgers, meaning a compromised or malicious SNS governance proposal that upgrades the ledger (and thus transiently controls it) could exploit this. The entry path is a standard ingress `update` call — no special network access is needed. [6](#0-5) 

### Recommendation
Add an ownership check inside `icrc152_burn_not_async` to ensure the `from` account owner either:
- equals the `caller`, **or**
- has granted an ICRC-2 allowance to the caller for at least `args.amount`.

Alternatively, restrict `icrc152_burn` so that a controller can only burn from the **minting account** (i.e., reclaim unissued supply), and require ICRC-2 approval for burning from user accounts. This matches the principle of least privilege and is consistent with how `icrc2_transfer_from` enforces allowances.

### Proof of Concept
Preconditions:
- A ledger is deployed with `feature_flags: Some(FeatureFlags { icrc2: true, icrc152: true })`.
- Alice holds 1,000,000 tokens in her account.
- Attacker controls a principal that is a controller of the ledger canister (e.g., via a governance proposal that installs a malicious upgrade, or directly if the attacker is already a controller).

Attack steps:
1. Attacker calls `icrc152_burn` on the ledger canister:
   ```
   icrc152_burn(Icrc152BurnArgs {
       from: Account { owner: alice_principal, subaccount: None },
       amount: 1_000_000,
       created_at_time: <current_time>,
       reason: Some("compliance"),
   })
   ```
2. The ledger checks `is_controller(attacker)` → passes.
3. No consent check for Alice is performed.
4. `apply_transaction` executes `Operation::AuthorizedBurn { from: alice_account, amount: 1_000_000 }`.
5. Alice's balance is reduced to 0. The burn is recorded on-chain as a `122burn` block. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L905-920)
```rust
fn icrc152_mint_not_async(
    caller: Principal,
    args: Icrc152MintArgs,
) -> Result<u64, Icrc152MintError> {
    let block_idx = Access::with_ledger_mut(|ledger| {
        if !ledger.feature_flags().icrc152 {
            return Err(Icrc152MintError::GenericError {
                error_code: Nat::from(0_u64),
                message: "ICRC-152 is not enabled".to_string(),
            });
        }
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152MintError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1043-1064)
```rust
        let now = TimeStamp::from_nanos_since_unix_epoch(ic_cdk::api::time());
        let tx = Transaction {
            operation: Operation::AuthorizedBurn {
                from: args.from,
                amount,
                caller: Some(caller),
                mthd: Some(MTHD_152_BURN.to_string()),
                reason: args.reason,
            },
            created_at_time: Some(args.created_at_time),
            memo: None,
        };
        let (block_idx, _) =
            apply_transaction(ledger, tx, now, Tokens::zero()).map_err(|err| match err {
                CoreTransferError::TxDuplicate { duplicate_of } => Icrc152BurnError::Duplicate {
                    duplicate_of: Nat::from(duplicate_of),
                },
                CoreTransferError::InsufficientFunds { balance } => {
                    Icrc152BurnError::InsufficientBalance {
                        balance: balance.into(),
                    }
                }
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

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L602-608)
```rust
impl FeatureFlags {
    const fn const_default() -> Self {
        Self {
            icrc2: true,
            icrc152: false,
        }
    }
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L638-639)
```text
  icrc152_mint : (Icrc152MintArgs) -> (Icrc152MintResult);
  icrc152_burn : (Icrc152BurnArgs) -> (Icrc152BurnResult);
```
