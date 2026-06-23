### Title
Controller Can Burn Any User's Tokens Without Consent via ICRC-152 `icrc152_burn` - (File: `rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The ICRC-152 extension to the ICRC-1 ledger introduces `icrc152_burn`, a privileged endpoint that allows any controller of the ledger canister to burn tokens from any user's account without requiring user consent, approval, or notification. This is a direct analog to the `sourceBurn` vulnerability in HolographERC721: a privileged entity (source contract → ledger controller) can destroy user assets without their permission, bypassing the normal approval/allowance mechanism entirely.

---

### Finding Description

The `icrc152_burn` update endpoint at `rs/ledger_suite/icrc1/ledger/src/main.rs` (lines 1088–1094) delegates to `icrc152_burn_not_async` (lines 998–1086). The authorization logic is:

1. Check that the `icrc152` feature flag is enabled on the ledger.
2. Check that `ic_cdk::api::is_controller(&caller)` returns `true`.

If both conditions are satisfied, the function constructs an `Operation::AuthorizedBurn` transaction targeting `args.from` — an **arbitrary user-supplied account** — and applies it directly to the ledger state via `apply_transaction`.

```rust
// rs/ledger_suite/icrc1/ledger/src/main.rs:1009-1013
if !ic_cdk::api::is_controller(&caller) {
    return Err(Icrc152BurnError::Unauthorized(
        "caller is not a controller".to_string(),
    ));
}
```

```rust
// rs/ledger_suite/icrc1/ledger/src/main.rs:1044-1051
let tx = Transaction {
    operation: Operation::AuthorizedBurn {
        from: args.from,   // ← any account, no user consent
        amount,
        caller: Some(caller),
        mthd: Some(MTHD_152_BURN.to_string()),
        reason: args.reason,
    },
    ...
};
```

The `AuthorizedBurn` operation is explicitly documented as bypassing the transfer API entirely. The in-memory ledger test comment states:

> "AuthorizedBurn is a privileged operation that bypasses the transfer API, so it must not deduct from any existing allowance even if the caller would match an approved spender." [1](#0-0) [2](#0-1) [3](#0-2) 

There is no user consent check, no allowance deduction, no notification mechanism, and no way for a token holder to opt out or prevent a controller from burning their balance.

The same pattern applies to `icrc152_mint` (lines 905–988), which allows a controller to mint tokens to any account without user interaction. [4](#0-3) 

---

### Impact Explanation

Any controller of an ICRC-152-enabled ICRC-1 ledger can:

- Burn the entire balance of any user account in a single call to `icrc152_burn`.
- Do so without the user's knowledge, consent, or any prior approval.
- Bypass all ICRC-2 allowance/approval protections — the `AuthorizedBurn` operation does not check or consume any allowance.

The `FeatureFlags` struct shows `icrc152` defaults to `false`, but it can be enabled at init time or via an upgrade:

```rust
// rs/ledger_suite/icrc1/ledger/src/lib.rs:595-608
pub struct FeatureFlags {
    pub icrc2: bool,
    #[serde(default)]
    pub icrc152: bool,
}
``` [5](#0-4) 

Once enabled, every token holder on that ledger is exposed to unilateral balance destruction by any controller. The `icrc152_burn` integration test confirms the controller can burn from a user account with no prior approval: [6](#0-5) 

---

### Likelihood Explanation

- **Opt-in surface**: The feature requires `icrc152: true` in `FeatureFlags`, so only ledgers that explicitly enable it are affected. However, the `UpgradeArgs` allow enabling it post-deployment, meaning a ledger that was safe at launch can become vulnerable after an upgrade.
- **Controller compromise**: On the IC, controllers are principals (users or canisters). If a controller's private key is leaked, or if a malicious canister is set as a controller, `icrc152_burn` becomes an immediate theft vector for all user balances.
- **Malicious deployer**: A ledger deployer who enables ICRC-152 and retains controller access can burn user tokens at will. Users holding tokens on such a ledger may not be aware of this risk.
- **Upgrade-time enablement**: The `UpgradeArgs.feature_flags` field allows enabling `icrc152` via a canister upgrade, which is controlled by the existing controllers — meaning the attack surface can be introduced silently after users have already deposited tokens. [7](#0-6) [8](#0-7) 

---

### Recommendation

1. **Require user consent**: Before a controller can burn a user's tokens via `icrc152_burn`, require the user to have pre-approved the controller (e.g., via an ICRC-2 `approve` call or a dedicated ICRC-152 consent mechanism).
2. **Time-lock burns**: Introduce a mandatory delay between a burn request and execution, giving users time to observe and react.
3. **Emit observable events**: Ensure that `AuthorizedBurn` operations are prominently surfaced in user-facing tooling so token holders can monitor for unauthorized burns.
4. **Restrict upgrade-time enablement**: Require a governance vote or multi-sig to enable `icrc152` via upgrade, preventing a single controller from silently enabling the feature after users have deposited tokens.
5. **Document the risk prominently**: At minimum, clearly disclose in the ledger's `icrc1_metadata` or `icrc1_supported_standards` response that ICRC-152 is enabled and that controllers can burn user balances without consent.

---

### Proof of Concept

```
1. Deploy an ICRC-1 ledger with:
   feature_flags: Some(FeatureFlags { icrc2: true, icrc152: true })

2. User A deposits 1,000,000 tokens to their account.

3. As a controller of the ledger, call:
   icrc152_burn({
     from: Account { owner: user_a_principal, subaccount: None },
     amount: 1_000_000,
     created_at_time: <current_time>,
     reason: Some("compliance"),
   })

4. User A's balance is now 0. No approval was requested. No allowance was consumed.
   The operation succeeds and is recorded as an AuthorizedBurn (btype "122burn") block.
```

This is confirmed by the integration test `test_icrc152_mint_and_burn` in `rs/ledger_suite/tests/sm-tests/src/lib.rs` (lines 6452–6535), which demonstrates a controller burning from a user account with no prior user interaction. [9](#0-8) [10](#0-9)

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

**File:** rs/ledger_suite/test_utils/in_memory_ledger/src/tests.rs (L574-578)
```rust
#[test]
fn should_not_consume_allowance_on_authorized_burn() {
    // AuthorizedBurn is a privileged operation that bypasses the transfer API,
    // so it must not deduct from any existing allowance even if the caller
    // would match an approved spender.
```

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

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L6452-6535)
```rust
pub fn test_icrc152_mint_and_burn<T>(ledger_wasm: Vec<u8>, encode_init_args: fn(InitArgs) -> T)
where
    T: CandidType,
{
    let p1 = PrincipalId::new_user_test_id(1);
    let (env, canister_id) = setup_icrc152(ledger_wasm, encode_init_args, vec![]);
    let controller = PrincipalId::new_anonymous();

    let supply_before = total_supply(&env, canister_id);
    assert_eq!(supply_before, 0);

    // --- Mint ---
    let mint_amount = 5_000_000_u64;
    let mint_result = icrc152_mint(
        &env,
        canister_id,
        controller,
        &Icrc152MintArgs {
            to: Account::from(p1.0),
            amount: Nat::from(mint_amount),
            created_at_time: now_nanos(&env),
            reason: Some("test mint".to_string()),
        },
    );
    let mint_block_idx = mint_result.expect("icrc152_mint should succeed");
    assert_eq!(mint_block_idx, Nat::from(0_u64));

    assert_eq!(balance_of(&env, canister_id, p1.0), mint_amount);
    assert_eq!(total_supply(&env, canister_id), mint_amount);

    // Verify block type via icrc3_get_blocks
    let blocks = icrc3_get_blocks(&env, canister_id, 0, 1);
    assert_eq!(blocks.blocks.len(), 1);
    let btype = match &blocks.blocks[0].block {
        ICRC3Value::Map(m) => m
            .iter()
            .find(|(k, _)| k.as_str() == "btype")
            .map(|(_, v)| v.clone()),
        other => panic!("expected Map block, got: {other:?}"),
    };
    assert_eq!(
        btype,
        Some(ICRC3Value::Text("122mint".to_string())),
        "mint block should have btype 122mint"
    );

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

    // Verify burn block type
    let blocks = icrc3_get_blocks(&env, canister_id, 1, 1);
    assert_eq!(blocks.blocks.len(), 1);
    let btype = match &blocks.blocks[0].block {
        ICRC3Value::Map(m) => m
            .iter()
            .find(|(k, _)| k.as_str() == "btype")
            .map(|(_, v)| v.clone()),
        other => panic!("expected Map block, got: {other:?}"),
    };
    assert_eq!(
        btype,
        Some(ICRC3Value::Text("122burn".to_string())),
        "burn block should have btype 122burn"
    );
}
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L140-150)
```text
type UpgradeArgs = record {
  metadata : opt vec record { text; MetadataValue };
  token_symbol : opt text;
  token_name : opt text;
  transfer_fee : opt nat;
  change_fee_collector : opt ChangeFeeCollector;
  max_memo_length : opt nat16;
  feature_flags : opt FeatureFlags;
  change_archive_options : opt ChangeArchiveOptions;
  index_principal : opt principal
};
```
