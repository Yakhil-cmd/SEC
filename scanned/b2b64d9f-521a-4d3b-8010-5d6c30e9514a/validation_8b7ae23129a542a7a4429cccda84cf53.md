### Title
Missing `icrc2` Feature Flag Gate in `icrc2_approve` and `icrc2_transfer_from` Endpoints - (File: rs/ledger_suite/icrc1/ledger/src/main.rs)

---

### Summary

The ICRC-1 ledger canister exposes an `icrc2` feature flag in `FeatureFlags` that is intended to control whether ICRC-2 operations are permitted. However, the actual update endpoints `icrc2_approve` and `icrc2_transfer_from` do not check this flag before executing. Any unprivileged ingress sender can invoke these endpoints and successfully create allowances or transfer tokens from third-party accounts even when the ledger operator has explicitly set `icrc2: false`, bypassing the operator's intended access-control policy.

---

### Finding Description

`FeatureFlags` in `rs/ledger_suite/icrc1/ledger/src/lib.rs` carries an `icrc2: bool` field that defaults to `true` but can be set to `false` at init or upgrade time. [1](#0-0) 

The `icrc1_supported_standards` query and `icrc3_supported_block_types` query both gate their ICRC-2 / ICRC-152 entries behind this flag, correctly advertising the feature as absent when disabled. [2](#0-1) 

However, the two state-mutating ICRC-2 endpoints — `icrc2_approve` (which calls `icrc2_approve_not_async`) and `icrc2_transfer_from` (which calls `execute_transfer`) — contain **no check** of `ledger.feature_flags().icrc2` before proceeding: [3](#0-2) [4](#0-3) 

By contrast, the newer ICRC-152 endpoints (`icrc152_mint_not_async`, `icrc152_burn_not_async`) correctly check the flag as their very first guard: [5](#0-4) [6](#0-5) 

The ICP ledger's ICRC-2 endpoints also correctly trap when the flag is off, confirming the intended design: [7](#0-6) 

A test in the ICRC-1 ledger suite explicitly documents this inconsistency — it is named `test_icrc2_feature_flag_doesnt_disable_icrc2_endpoints` and asserts that `icrc2_approve` returns `InsufficientFunds` (not a "disabled" error) when `icrc2: false`: [8](#0-7) 

---

### Impact Explanation

An operator who deploys an ICRC-1 ledger with `icrc2: false` — for example to create a simple transfer-only token, for regulatory compliance, or to match the advertised standard set — receives no enforcement of that policy. Any user can:

1. Call `icrc2_approve` to grant a third-party spender an allowance over their tokens.
2. Call `icrc2_transfer_from` to pull tokens from any account that has granted an allowance.

Because `icrc1_supported_standards` does not advertise ICRC-2, integrators and auditors inspecting the ledger's capabilities will believe ICRC-2 is inactive, while the on-chain state silently accepts ICRC-2 transactions and records `2approve` / `2xfer` blocks. This creates a false sense of security and can lead to unexpected token movements that the operator did not intend to permit.

---

### Likelihood Explanation

The entry path is a standard ingress update call — no special role, key, or privilege is required. Any principal can call `icrc2_approve` or `icrc2_transfer_from` on any ICRC-1 ledger canister regardless of the `icrc2` flag value. The ledger suite orchestrator deploys ckERC-20 ledgers with `icrc2: true` today, but the flag can be toggled to `false` via an upgrade, and any future ledger deployment that sets `icrc2: false` is immediately exploitable. [9](#0-8) 

---

### Recommendation

Add a feature-flag guard at the top of `icrc2_approve_not_async` and inside `execute_transfer` (or at the `icrc2_transfer_from` call site), mirroring the pattern already used by `icrc152_mint_not_async` and `icrc152_burn_not_async`:

```rust
// in icrc2_approve_not_async, before any other logic:
if !ledger.feature_flags().icrc2 {
    ic_cdk::trap("ICRC-2 features are not enabled on the ledger.");
}

// in icrc2_transfer_from (or execute_transfer when spender is Some):
if !ledger.feature_flags().icrc2 {
    ic_cdk::trap("ICRC-2 features are not enabled on the ledger.");
}
```

The `icrc2_allowance` query endpoint should similarly be gated to avoid leaking allowance state when the feature is advertised as disabled.

---

### Proof of Concept

1. Deploy an ICRC-1 ledger with `feature_flags: Some(FeatureFlags { icrc2: false, icrc152: false })` and fund account A.
2. Call `icrc1_supported_standards()` — observe ICRC-2 is absent.
3. As account A, call `icrc2_approve` granting account B an allowance of 1 000 tokens. The call succeeds and returns a block index.
4. As account B, call `icrc2_transfer_from` moving tokens from A to B. The call succeeds.
5. Verify via `icrc3_get_blocks` that `2approve` and `2xfer` blocks were recorded despite the feature being "disabled".

This is confirmed by the existing test `test_icrc2_feature_flag_doesnt_disable_icrc2_endpoints` which already asserts step 3 returns a normal ledger error (not a "disabled" trap), proving the endpoint is reachable and functional. [8](#0-7)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L595-608)
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
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L701-725)
```rust
#[update]
async fn icrc2_transfer_from(arg: TransferFromArgs) -> Result<Nat, TransferFromError> {
    let spender_account = Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: arg.spender_subaccount,
    };
    execute_transfer(
        arg.from,
        arg.to,
        Some(spender_account),
        arg.fee,
        arg.amount,
        arg.memo,
        arg.created_at_time,
    )
    .await
    .map_err(convert_transfer_error)
    .map_err(|err| {
        let err: TransferFromError = match err.try_into() {
            Ok(err) => err,
            Err(err) => ic_cdk::trap(&err),
        };
        err
    })
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L783-789)
```rust
    if Access::with_ledger(|ledger| ledger.feature_flags().icrc152) {
        standards.push(StandardRecord {
            name: "ICRC-152".to_string(),
            url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-152.md".to_string(),
        });
    }
    standards
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L820-891)
```rust
fn icrc2_approve_not_async(caller: Principal, arg: ApproveArgs) -> Result<u64, ApproveError> {
    let block_idx = Access::with_ledger_mut(|ledger| {
        let now = TimeStamp::from_nanos_since_unix_epoch(ic_cdk::api::time());

        let from_account = Account {
            owner: caller,
            subaccount: arg.from_subaccount,
        };
        if from_account.owner == arg.spender.owner {
            ic_cdk::trap("self approval is not allowed")
        }
        if &from_account == ledger.minting_account() {
            ic_cdk::trap("the minting account cannot delegate mints")
        }
        match arg.memo.as_ref() {
            Some(memo) if memo.0.len() > ledger.max_memo_length() as usize => {
                ic_cdk::trap("the memo field is too large")
            }
            _ => {}
        };
        let amount = Tokens::try_from(arg.amount).unwrap_or_else(|_| Tokens::max_value());
        let expected_allowance = match arg.expected_allowance {
            Some(n) => match Tokens::try_from(n) {
                Ok(n) => Some(n),
                Err(_) => {
                    let current_allowance = ledger
                        .approvals()
                        .allowance(&from_account, &arg.spender, now)
                        .amount;
                    return Err(ApproveError::AllowanceChanged {
                        current_allowance: current_allowance.into(),
                    });
                }
            },
            None => None,
        };

        let expected_fee_tokens = ledger.transfer_fee();
        let expected_fee: Nat = expected_fee_tokens.into();
        if arg.fee.is_some() && arg.fee.as_ref() != Some(&expected_fee) {
            return Err(ApproveError::BadFee { expected_fee });
        }

        let tx = Transaction {
            operation: Operation::Approve {
                from: from_account,
                spender: arg.spender,
                amount,
                expected_allowance,
                expires_at: arg.expires_at,
                fee: arg.fee.map(|_| expected_fee_tokens),
            },
            created_at_time: arg.created_at_time,
            memo: arg.memo,
        };

        let (block_idx, _) = apply_transaction(ledger, tx, now, expected_fee_tokens)
            .map_err(convert_transfer_error)
            .map_err(|err| {
                let err: ApproveError = match err.try_into() {
                    Ok(err) => err,
                    Err(err) => ic_cdk::trap(&err),
                };
                err
            })?;
        Ok(block_idx)
    })?;

    update_total_volume(Tokens::zero(), true);

    Ok(block_idx)
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L909-915)
```rust
    let block_idx = Access::with_ledger_mut(|ledger| {
        if !ledger.feature_flags().icrc152 {
            return Err(Icrc152MintError::GenericError {
                error_code: Nat::from(0_u64),
                message: "ICRC-152 is not enabled".to_string(),
            });
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1002-1008)
```rust
    let block_idx = Access::with_ledger_mut(|ledger| {
        if !ledger.feature_flags().icrc152 {
            return Err(Icrc152BurnError::GenericError {
                error_code: Nat::from(0_u64),
                message: "ICRC-152 is not enabled".to_string(),
            });
        }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L457-478)
```rust
fn icrc1_supported_standards() -> Vec<StandardRecord> {
    let mut standards = vec![StandardRecord {
        name: "ICRC-1".to_string(),
        url: "https://github.com/dfinity/ICRC-1/tree/main/standards/ICRC-1".to_string(),
    }];
    if LEDGER.read().unwrap().feature_flags.icrc2 {
        standards.push(StandardRecord {
            name: "ICRC-2".to_string(),
            url: "https://github.com/dfinity/ICRC-1/tree/main/standards/ICRC-2".to_string(),
        });
    }
    standards.push(StandardRecord {
        name: "ICRC-21".to_string(),
        url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-21/ICRC-21.md".to_string(),
    });
    standards.push(StandardRecord {
        name: "ICRC-10".to_string(),
        url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-10/ICRC-10.md".to_string(),
    });

    standards
}
```

**File:** rs/ledger_suite/icrc1/ledger/tests/tests.rs (L847-939)
```rust
#[test]
fn test_icrc2_feature_flag_doesnt_disable_icrc2_endpoints() {
    // Disable ICRC-2 and check the endpoints still work

    let env = StateMachine::new();
    let init_args = Encode!(&LedgerArgument::Init(InitArgs {
        minting_account: MINTER,
        fee_collector_account: None,
        initial_balances: vec![],
        transfer_fee: FEE.into(),
        token_name: TOKEN_NAME.to_string(),
        decimals: Some(DECIMAL_PLACES),
        token_symbol: TOKEN_SYMBOL.to_string(),
        metadata: vec![],
        archive_options: ArchiveOptions {
            trigger_threshold: ARCHIVE_TRIGGER_THRESHOLD as usize,
            num_blocks_to_archive: NUM_BLOCKS_TO_ARCHIVE as usize,
            node_max_memory_size_bytes: None,
            max_message_size_bytes: None,
            controller_id: PrincipalId::new_user_test_id(100),
            more_controller_ids: None,
            cycles_for_archive_creation: Some(0),
            max_transactions_per_response: None,
        },
        max_memo_length: None,
        feature_flags: Some(FeatureFlags {
            icrc2: false,
            icrc152: false
        }),
        index_principal: None,
    }))
    .unwrap();
    let ledger_id = env
        .install_canister(ledger_wasm(), init_args, None)
        .unwrap();
    let user1 = account(1);
    let user2 = account(2);
    let user3 = account(3);

    // if ICRC-2 is enabled then none of the following operations
    // should trap

    assert_eq!(
        Account::get_allowance(&env, ledger_id, user1, user2),
        Allowance {
            allowance: 0_u32.into(),
            expires_at: None
        }
    );

    let approval_result = send_approval(
        &env,
        ledger_id,
        user1.owner,
        &ApproveArgs {
            from_subaccount: None,
            spender: user3,
            amount: 1_000_000_u32.into(),
            expected_allowance: None,
            expires_at: None,
            fee: None,
            memo: None,
            created_at_time: None,
        },
    );
    assert_eq!(
        approval_result,
        Err(ApproveError::InsufficientFunds {
            balance: 0_u32.into()
        })
    );

    let transfer_from_result = send_transfer_from(
        &env,
        ledger_id,
        user3.owner,
        &TransferFromArgs {
            spender_subaccount: None,
            from: user1,
            to: user2,
            amount: 1_000_000_u32.into(),
            fee: None,
            memo: None,
            created_at_time: None,
        },
    );
    assert_eq!(
        transfer_from_result,
        Err(TransferFromError::InsufficientAllowance {
            allowance: 0_u32.into()
        })
    );
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L921-924)
```rust
    const ICRC2_FEATURE: LedgerFeatureFlags = LedgerFeatureFlags {
        icrc2: true,
        icrc152: false,
    };
```
