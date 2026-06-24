### Title
Controller Can Immediately Drain Any User's Tokens via `icrc152_burn` Without Timelock Protection - (File: rs/ledger_suite/icrc1/ledger/src/main.rs)

### Summary
The ICRC-1 ledger canister exposes `icrc152_burn` and `icrc152_mint` as privileged update endpoints gated solely by `ic_cdk::api::is_controller`. Any canister controller can immediately burn tokens from **any user's account** or mint arbitrary tokens to any account, with no timelock, no governance delay, and no user consent. This is a direct analog to the EVM report's finding that privileged roles can drain protocol funds immediately without timelock protection.

### Finding Description
In `rs/ledger_suite/icrc1/ledger/src/main.rs`, the `icrc152_burn_not_async` function checks only two conditions before burning tokens from an arbitrary account:

1. The `icrc152` feature flag is enabled (`ledger.feature_flags().icrc152`)
2. The caller is a controller (`ic_cdk::api::is_controller(&caller)`) [1](#0-0) 

Once both conditions are satisfied, the function constructs an `AuthorizedBurn` operation targeting `args.from` — an **arbitrary account specified by the caller**, not the caller's own account — and applies it directly to the ledger state: [2](#0-1) 

The `Operation::AuthorizedBurn` path in `rs/ledger_suite/icrc1/src/lib.rs` calls `balances_mut().burn(from, amount)` with no further authorization checks: [3](#0-2) 

Similarly, `icrc152_mint_not_async` allows a controller to mint tokens to any account: [4](#0-3) 

The `icrc152` feature flag is a runtime toggle in `FeatureFlags` that can be enabled at init time or via `UpgradeArgs`: [5](#0-4) 

The `upgrade()` function on the ledger state applies `feature_flags` from upgrade args without restriction: [6](#0-5) 

The public endpoint is exposed unconditionally in the canister interface: [7](#0-6) 

### Impact Explanation
A canister controller of an ICRC-1 ledger with `icrc152: true` can:

- **Burn tokens from any user's account** without that user's consent, effectively stealing their balance.
- **Mint unlimited tokens** to any account, inflating supply and devaluing all existing holders.

Both operations execute **immediately** with no timelock, no waiting period, and no on-chain delay that would allow users to exit before the attack completes. This is the exact analog to the EVM report: a privileged role can drain all protocol funds in a single transaction.

### Likelihood Explanation
The `icrc152` feature is opt-in (disabled by default), but once enabled, the attack surface is permanently open for the lifetime of the canister. The ledger suite orchestrator (`vxkom-oyaaa-aaaar-qafda-cai`) is listed as a controller of ckETH ledger and archive canisters: [8](#0-7) 

If the orchestrator canister is itself upgraded maliciously (e.g., via a compromised NNS proposal or a bug in the orchestrator), it could call `icrc152_burn` on any user's ledger balance. The orchestrator's `icrc1_ledger_init_arg` currently initializes ledgers with `icrc152: false`: [9](#0-8) 

However, a future upgrade enabling `icrc152: true` via `UpgradeArgs` would immediately expose all user balances to controller-level drain with no timelock protection.

### Recommendation
1. **Add a timelock mechanism** for `icrc152_burn` and `icrc152_mint` operations, analogous to the EVM report's recommendation. Any controller-initiated burn/mint should be queued with a mandatory delay (e.g., 24–48 hours) during which users can observe and exit.
2. **Restrict `icrc152_burn` to the caller's own account** or require explicit per-account authorization from the account owner, rather than allowing arbitrary `from` accounts.
3. **Document prominently** in protocol docs that enabling `icrc152: true` grants controllers the ability to burn any user's tokens without consent.
4. **Separate the burn-from-any-account capability** from the controller role; consider a dedicated, time-locked admin role for compliance burns.

### Proof of Concept
Attacker controls (or compromises) a canister `C` that is a controller of an ICRC-1 ledger with `icrc152: true`.

1. `C` calls `icrc152_burn` with `args.from = victim_account`, `args.amount = victim_balance`.
2. `icrc152_burn_not_async` checks `is_controller(C)` → passes.
3. `Operation::AuthorizedBurn { from: victim_account, amount: victim_balance, ... }` is applied.
4. `balances_mut().burn(&victim_account, victim_balance)` executes immediately.
5. Victim's entire balance is destroyed in a single round, with no timelock window for the victim to react. [10](#0-9)

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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L998-1086)
```rust
fn icrc152_burn_not_async(
    caller: Principal,
    args: Icrc152BurnArgs,
) -> Result<u64, Icrc152BurnError> {
    let block_idx = Access::with_ledger_mut(|ledger| {
        if !ledger.feature_flags().icrc152 {
            return Err(Icrc152BurnError::GenericError {
                error_code: Nat::from(0_u64),
                message: "ICRC-152 is not enabled".to_string(),
            });
        }
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
                CoreTransferError::TxTooOld { .. } => Icrc152BurnError::GenericError {
                    error_code: Nat::from(0_u64),
                    message: "transaction too old".to_string(),
                },
                CoreTransferError::TxCreatedInFuture { .. } => Icrc152BurnError::GenericError {
                    error_code: Nat::from(0_u64),
                    message: "transaction created in the future".to_string(),
                },
                CoreTransferError::TxThrottled => Icrc152BurnError::GenericError {
                    error_code: Nat::from(0_u64),
                    message: "temporarily unavailable".to_string(),
                },
                other => Icrc152BurnError::GenericError {
                    error_code: Nat::from(0_u64),
                    message: format!("unexpected error: {:?}", other),
                },
            })?;
        update_total_volume(amount, false);
        Ok(block_idx)
    })?;
    Ok(block_idx)
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

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L952-960)
```rust
        if let Some(feature_flags) = args.feature_flags {
            if !feature_flags.icrc2 {
                log!(
                    sink,
                    "[ledger] feature flag icrc2 is deprecated and won't disable ICRC-2 anymore"
                );
            }
            self.feature_flags = feature_flags;
        }
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L638-639)
```text
  icrc152_mint : (Icrc152MintArgs) -> (Icrc152MintResult);
  icrc152_burn : (Icrc152BurnArgs) -> (Icrc152BurnResult);
```

**File:** rs/ethereum/cketh/mainnet/cketh_ledger_settings_2024_09_06.md (L1-4)
```markdown
# Update controllers of the ckETH ledger canister

This proposal changes the controllers of the ckETH ledger canister to add, in addition to the NNS root ([`r7inp-6aaaa-aaaaa-aaabq-cai`](https://dashboard.internetcomputer.org/canister/r7inp-6aaaa-aaaaa-aaabq-cai)), the ledger suite orchestrator ([`vxkom-oyaaa-aaaar-qafda-cai`](https://dashboard.internetcomputer.org/canister/vxkom-oyaaa-aaaar-qafda-cai)) as a controller.
A future upgrade proposal targeting the ledger suite orchestrator ([`vxkom-oyaaa-aaaar-qafda-cai`](https://dashboard.internetcomputer.org/canister/vxkom-oyaaa-aaaar-qafda-cai)) will add the ckETH ledger to the canisters managed by the ledger suite orchestrator. The aim is that the ckETH ledger suite, similarly to the other ckERC20 ledger suites already managed by the ledger suite orchestrator, will be managed by the orchestrator to facilitate the management of those canisters (e.g., cycles top-up and upgrades).
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L921-924)
```rust
    const ICRC2_FEATURE: LedgerFeatureFlags = LedgerFeatureFlags {
        icrc2: true,
        icrc152: false,
    };
```
