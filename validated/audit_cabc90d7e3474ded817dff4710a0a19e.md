### Title
ICRC-1 Ledger `icrc2` Feature Flag Disable Mechanism Is Non-Functional — (File: `rs/ledger_suite/icrc1/ledger/src/lib.rs`)

---

### Summary

The ICRC-1 ledger's `FeatureFlags` struct exposes an `icrc2: bool` field that is accepted in both init and upgrade arguments, implying that ICRC-2 can be disabled. However, the flag is deprecated: setting `icrc2: false` only emits a log warning and has no effect on ICRC-2 endpoint behavior. The mechanism to disable ICRC-2 is declared in the interface but is completely non-functional, leaving ledger controllers with no emergency protection against a critical ICRC-2 vulnerability.

---

### Finding Description

`FeatureFlags` in the ICRC-1 ledger declares two boolean fields: `icrc2` and `icrc152`. [1](#0-0) 

The `icrc152` field properly gates ICRC-152 functionality — when `icrc152: false`, the `icrc152_mint` and `icrc152_burn` endpoints return a `GenericError("not enabled")` and ICRC-152 is excluded from `icrc1_supported_standards`. [2](#0-1) 

By contrast, the `icrc2` field is deprecated. In both `from_init_args` and `upgrade`, setting `icrc2: false` only logs a deprecation warning; the flag is stored in state but is **never checked** in any ICRC-2 endpoint (`icrc2_approve`, `icrc2_transfer_from`, `icrc2_allowance`). ICRC-2 is also unconditionally included in `icrc1_supported_standards` regardless of the flag value. [3](#0-2) [4](#0-3) 

This is confirmed by the test `test_icrc2_feature_flag_doesnt_disable_icrc2_endpoints`, which explicitly verifies that `approve`, `allowance`, and `transfer_from` all succeed even when the ledger is initialized with `icrc2: false`. [5](#0-4) 

Note the asymmetry: the ICP ledger's `icrc2` flag **is** functional and properly disables ICRC-2 when set to `false`. [6](#0-5) 

---

### Impact Explanation

If a critical vulnerability is discovered in the ICRC-2 implementation of an ICRC-1 ledger (e.g., a logic error in `icrc2_approve` or `icrc2_transfer_from` that allows unauthorized token transfers or allowance manipulation), the ledger controller has no fast emergency path to disable ICRC-2. Calling upgrade with `feature_flags: Some(FeatureFlags { icrc2: false, icrc152: false })` silently does nothing. The only recourse is a full canister upgrade, which on NNS-controlled ledgers requires a governance proposal and introduces significant delay during which the vulnerability remains exploitable. The interface actively misleads operators into believing the disable mechanism works.

---

### Likelihood Explanation

Medium. The `icrc2: bool` field remains present in the public Candid interface (`ledger.did`) and in the `FeatureFlags` type accepted by both `init` and `upgrade` arguments. An operator responding to an emergency would naturally attempt to set `icrc2: false` as the first mitigation step, observe no error, and incorrectly believe ICRC-2 has been disabled. The ICP ledger's working `icrc2` flag reinforces this expectation. The scenario requires a pre-existing ICRC-2 bug to be impactful, but the non-functional flag is a standing condition on every deployed ICRC-1 ledger. [7](#0-6) 

---

### Recommendation

Either:

1. **Remove `icrc2` from `FeatureFlags`** entirely (and from the Candid interface) to eliminate the misleading field and prevent operators from believing they can disable ICRC-2.
2. **Re-implement the disable gate**: check `self.feature_flags.icrc2` at the top of `icrc2_approve`, `icrc2_transfer_from`, and `icrc2_allowance`, returning an appropriate error when `false`, consistent with how `icrc152` is gated. Also gate the ICRC-2 entry in `icrc1_supported_standards` on the flag value.

---

### Proof of Concept

1. Deploy an ICRC-1 ledger with `feature_flags: Some(FeatureFlags { icrc2: false, icrc152: false })`.
2. Call `icrc2_approve` — it succeeds (returns `InsufficientFunds` or a valid block index, not a "disabled" error).
3. Call `icrc2_allowance` — it returns a valid allowance response.
4. Call `icrc1_supported_standards` — ICRC-2 is listed as supported.
5. Upgrade the ledger with `feature_flags: Some(FeatureFlags { icrc2: false, icrc152: false })` — the upgrade log shows `"feature flag icrc2 is deprecated and won't disable ICRC-2 anymore"` but ICRC-2 endpoints remain fully operational.

The `icrc2: false` flag has no observable effect on any ICRC-2 endpoint, confirming the disable mechanism is unreachable. [8](#0-7)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L595-615)
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

impl Default for FeatureFlags {
    fn default() -> Self {
        Self::const_default()
    }
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L694-699)
```rust
        if feature_flags.as_ref().map(|ff| ff.icrc2) == Some(false) {
            log!(
                sink,
                "[ledger] feature flag icrc2 is deprecated and won't disable ICRC-2 anymore"
            );
        }
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L909-911)
```rust
    pub fn feature_flags(&self) -> &FeatureFlags {
        &self.feature_flags
    }
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L952-959)
```rust
        if let Some(feature_flags) = args.feature_flags {
            if !feature_flags.icrc2 {
                log!(
                    sink,
                    "[ledger] feature flag icrc2 is deprecated and won't disable ICRC-2 anymore"
                );
            }
            self.feature_flags = feature_flags;
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

**File:** rs/ledger_suite/icrc1/ledger/tests/tests.rs (L847-940)
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

**File:** rs/ledger_suite/icp/ledger/tests/tests.rs (L1325-1418)
```rust
#[test]
fn test_feature_flags() {
    let ledger_wasm = ledger_wasm();

    let from = PrincipalId::new_user_test_id(1);
    let spender = PrincipalId::new_user_test_id(2);
    let to = PrincipalId::new_user_test_id(3);

    let env = StateMachine::new();
    let mut initial_balances = HashMap::new();
    initial_balances.insert(Account::from(from.0).into(), Tokens::from_e8s(100_000));
    let payload = LedgerCanisterInitPayload::builder()
        .minting_account(MINTER.into())
        .icrc1_minting_account(MINTER)
        .initial_values(initial_balances)
        .transfer_fee(Tokens::from_e8s(10_000))
        .token_symbol_and_name("ICP", "Internet Computer")
        .feature_flags(FeatureFlags { icrc2: false })
        .build()
        .unwrap();
    let canister_id = env
        .install_canister(ledger_wasm.clone(), Encode!(&payload).unwrap(), None)
        .expect("Unable to install the Ledger canister with the new init");

    let approve_args = default_approve_args(spender.0, 150_000);
    let allowance_args = AllowanceArgs {
        account: from.0.into(),
        spender: spender.0.into(),
    };
    let transfer_from_args = default_transfer_from_args(from.0, to.0, 10_000);

    expect_icrc2_disabled(
        &env,
        from,
        canister_id,
        &approve_args,
        &allowance_args,
        &transfer_from_args,
    );

    env.upgrade_canister(
        canister_id,
        ledger_wasm.clone(),
        Encode!(&LedgerCanisterPayload::Upgrade(Some(UpgradeArgs {
            icrc1_minting_account: None,
            feature_flags: Some(FeatureFlags { icrc2: false }),
            change_archive_options: None,
        })))
        .unwrap(),
    )
    .unwrap();

    expect_icrc2_disabled(
        &env,
        from,
        canister_id,
        &approve_args,
        &allowance_args,
        &transfer_from_args,
    );

    env.upgrade_canister(
        canister_id,
        ledger_wasm,
        Encode!(&LedgerCanisterPayload::Upgrade(Some(UpgradeArgs {
            icrc1_minting_account: None,
            feature_flags: Some(FeatureFlags { icrc2: true }),
            change_archive_options: None,
        })))
        .unwrap(),
    )
    .unwrap();

    let mut standards = vec![];
    for standard in supported_standards(&env, canister_id) {
        standards.push(standard.name);
    }
    standards.sort();
    assert_eq!(standards, vec!["ICRC-1", "ICRC-10", "ICRC-2", "ICRC-21"]);

    let block_index =
        send_approval(&env, canister_id, from.0, &approve_args).expect("approval failed");
    assert_eq!(block_index, 1);
    let allowance = Account::get_allowance(&env, canister_id, from.0, spender.0);
    assert_eq!(allowance.allowance.0.to_u64().unwrap(), 150_000);
    let block_index = send_transfer_from(&env, canister_id, spender.0, &transfer_from_args)
        .expect("transfer_from failed");
    assert_eq!(block_index, 2);
    let allowance = Account::get_allowance(&env, canister_id, from.0, spender.0);
    assert_eq!(allowance.allowance.0.to_u64().unwrap(), 130_000);
    assert_eq!(balance_of(&env, canister_id, from.0), 70_000);
    assert_eq!(balance_of(&env, canister_id, to.0), 10_000);
    assert_eq!(balance_of(&env, canister_id, spender.0), 0);
}
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L94-97)
```text
type FeatureFlags = record {
  icrc2 : bool;
  icrc152 : bool
};
```
