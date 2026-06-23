### Title
ICRC-152 `icrc152_burn` Allows Any Ledger Controller to Burn Tokens from Any Arbitrary Account Without the Account Owner's Consent - (File: rs/ledger_suite/icrc1/ledger/src/main.rs)

### Summary
The `icrc152_burn` endpoint in the ICRC-1 ledger canister allows any canister controller to burn tokens from **any arbitrary account** (`args.from`) without requiring the account owner's approval or consent. This is the direct Internet Computer analog of the reported `requestRedeem` bug: instead of burning from `msg.sender`, the burn target is a caller-supplied parameter with no ownership check.

### Finding Description
The `icrc152_burn` function in `rs/ledger_suite/icrc1/ledger/src/main.rs` accepts an `Icrc152BurnArgs` struct containing a `from: Account` field that specifies **which account to burn tokens from**. The only authorization check performed is `ic_cdk::api::is_controller(&caller)` — i.e., whether the ingress sender is a controller of the ledger canister. There is no check that `args.from.owner == caller` or that the account owner has approved the burn. [1](#0-0) 

Once the controller check passes, the function directly uses `args.from` as the burn source: [2](#0-1) 

The `Icrc152BurnArgs` type makes `from` a fully caller-controlled parameter: [3](#0-2) 

The `icrc152_burn` public update endpoint passes `msg_caller()` for the controller check but forwards the caller-supplied `args.from` unchanged into the `AuthorizedBurn` operation: [4](#0-3) 

The `AuthorizedBurn` operation then directly debits the `from` account with no allowance or consent check: [5](#0-4) 

### Impact Explanation
Any canister that is a controller of an ICRC-152-enabled ledger can burn tokens from **any non-minting, non-anonymous account** on that ledger without the account owner's knowledge or approval. This is a direct ledger conservation violation: token balances of innocent users can be reduced to zero by a controller canister. On the Internet Computer, ledger controllers include governance canisters (e.g., NNS root, SNS governance) and any canister listed as a controller at deployment or upgrade time. If any such controller canister is compromised or acts maliciously, it can drain all user balances. The feature is gated behind `icrc152: true` in `FeatureFlags`, but once enabled, the attack surface is fully open to all controllers. [6](#0-5) 

### Likelihood Explanation
**Medium.** The `icrc152` feature flag is `false` by default, limiting exposure to ledgers that explicitly opt in. However, the ICRC-152 standard is designed for compliance/regulatory use cases (e.g., freezing or clawing back tokens), and any ledger that enables it is immediately vulnerable to any of its controllers burning from any account. The controller list is set at canister install/upgrade time and may include multiple parties. A compromised or malicious controller canister — or a governance proposal that adds a malicious controller — can exploit this without any on-chain consent from affected token holders. [7](#0-6) 

### Recommendation
Add an ownership or allowance check inside `icrc152_burn_not_async` to ensure the burn target is either:
1. The caller's own account (`args.from.owner == caller`), or
2. An account that has explicitly approved the controller to burn on its behalf (via an allowance/approval mechanism).

At minimum, document clearly that any controller can burn from any account, and consider whether the ICRC-152 design intent requires an explicit per-account consent mechanism rather than blanket controller authority. The analog fix in the original report was to enforce burning from `msg.sender` rather than a receiver parameter. [8](#0-7) 

### Proof of Concept
1. Deploy an ICRC-1 ledger with `feature_flags: Some(FeatureFlags { icrc2: true, icrc152: true })`.
2. Mint tokens to victim account `{ owner: victim_principal, subaccount: None }`.
3. From any canister that is a controller of the ledger, call `icrc152_burn` with:
   ```
   Icrc152BurnArgs {
       from: Account { owner: victim_principal, subaccount: None },
       amount: <victim's full balance>,
       created_at_time: <current_time>,
       reason: Some("compliance"),
   }
   ```
4. The ledger accepts the call (controller check passes), burns the victim's tokens, and records an `AuthorizedBurn` block — all without any action or approval from `victim_principal`.

The existing integration test `test_icrc152_mint_and_burn` demonstrates this exact pattern: the controller burns from `p1`'s account without `p1` ever approving anything. [9](#0-8)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1009-1051)
```rust
        if !ic_cdk::api::is_controller(&caller) {
            return Err(Icrc152BurnError::Unauthorized(
                "caller is not a controller".to_string(),
            ));
        }
        if args.amount == 0_u64 {
            return Err(Icrc152BurnError::GenericError {
                error_code: Nat::from(0_u64),
                message: "amount must be greater than 0".to_string(),
            });
        }
        let amount =
            Tokens::try_from(args.amount.clone()).map_err(|_| Icrc152BurnError::GenericError {
                error_code: Nat::from(0_u64),
                message: "amount is too large".to_string(),
            })?;
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
        if let Some(ref reason) = args.reason
            && reason.len() > MAX_REASON_LENGTH
        {
            return Err(Icrc152BurnError::GenericError {
                error_code: Nat::from(0_u64),
                message: format!("reason must be at most {} bytes", MAX_REASON_LENGTH),
            });
        }
        let now = TimeStamp::from_nanos_since_unix_epoch(ic_cdk::api::time());
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

**File:** packages/icrc-ledger-types/src/icrc152/mod.rs (L22-28)
```rust
#[derive(Clone, Debug, CandidType, Deserialize)]
pub struct Icrc152BurnArgs {
    pub from: Account,
    pub amount: Nat,
    pub created_at_time: u64,
    pub reason: Option<String>,
}
```

**File:** rs/ledger_suite/icrc1/src/lib.rs (L562-564)
```rust
            Operation::AuthorizedBurn { from, amount, .. } => {
                context.balances_mut().burn(from, amount.clone())?;
            }
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L595-609)
```rust
#[derive(Clone, Eq, PartialEq, Debug, CandidType, Deserialize, Serialize)]
pub struct FeatureFlags {
    pub icrc2: bool,
    #[serde(default)]
    pub icrc152: bool,
}

impl FeatureFlags {
    const fn const_default() -> Self {
        Self {
            icrc2: true,
            icrc152: false,
        }
    }
}
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L6498-6518)
```rust
    // --- Burn ---
    let burn_amount = 2_000_000_u64;
    let burn_result = icrc152_burn(
        &env,
        canister_id,
        controller,
        &Icrc152BurnArgs {
            from: Account::from(p1.0),
            amount: Nat::from(burn_amount),
            created_at_time: now_nanos(&env),
            reason: Some("test burn".to_string()),
        },
    );
    let burn_block_idx = burn_result.expect("icrc152_burn should succeed");
    assert_eq!(burn_block_idx, Nat::from(1_u64));

    assert_eq!(
        balance_of(&env, canister_id, p1.0),
        mint_amount - burn_amount
    );
    assert_eq!(total_supply(&env, canister_id), mint_amount - burn_amount);
```
