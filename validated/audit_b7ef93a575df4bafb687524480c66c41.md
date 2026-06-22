### Title
ICRC-1 Ledger Unconditionally Advertises ICRC-106 Compliance When Index Principal Is Not Configured - (File: rs/ledger_suite/icrc1/ledger/src/main.rs)

### Summary
The ICRC-1 ledger canister unconditionally includes `"ICRC-106"` in both `icrc1_supported_standards` and `icrc10_supported_standards` responses regardless of whether an index principal has been configured. When no index principal is set, `icrc106_get_index_principal` returns `Err(Icrc106Error::IndexPrincipalNotSet)`, meaning the canister falsely claims full ICRC-106 compliance to any caller who queries the standards list.

### Finding Description
In `rs/ledger_suite/icrc1/ledger/src/main.rs`, the `supported_standards()` function unconditionally pushes `"ICRC-106"` into the returned standards vector: [1](#0-0) 

This same function is returned by both `icrc1_supported_standards` and `icrc10_supported_standards`: [2](#0-1) 

However, the actual ICRC-106 endpoint `icrc106_get_index_principal` returns an error when no index principal is configured: [3](#0-2) 

The error type `Icrc106Error::IndexPrincipalNotSet` is defined in the types package: [4](#0-3) 

By contrast, ICRC-152 is conditionally advertised only when the `icrc152` feature flag is enabled, demonstrating that the codebase already has a pattern for conditional standard advertisement: [5](#0-4) 

The `InitArgs` struct shows `index_principal` is optional and defaults to `None`: [6](#0-5) 

The test suite explicitly validates and enshrines this false-compliance behavior under the name `test_icrc106_supported_even_if_index_not_set`: [7](#0-6) 

The test in the ledger crate is even named `test_icrc106_unsupported_if_index_not_set` but calls the function that asserts ICRC-106 IS advertised, revealing the naming contradiction: [8](#0-7) 

### Impact Explanation
Any unprivileged caller (wallet, dApp, aggregator, or chain-fusion integration) that calls `icrc10_supported_standards` or `icrc1_supported_standards` on a ledger deployed without an index principal will receive a standards list that includes `"ICRC-106"`. Acting on this, the caller will invoke `icrc106_get_index_principal` and receive `IndexPrincipalNotSet`. ICRC-10 is the discovery mechanism by which ecosystem tooling determines which standards a canister supports; a false entry in this list breaks the contract between the ledger and its consumers. Wallets and dApps that use the standards list to route users to the index canister for account transaction history will silently fail. The `icrc106:index_principal` metadata key will also be absent from `icrc1_metadata`, creating a second inconsistency observable by any query caller. [9](#0-8) 

### Likelihood Explanation
Every ICRC-1 ledger deployed without an explicit `index_principal` in its `InitArgs` — which is the default, as `index_principal: None` is the default in `InitArgsBuilder::for_tests()` and the field is `Option<Principal>` — will exhibit this behavior. Any SNS-deployed token ledger that has not been upgraded to set the index principal will advertise ICRC-106 while being unable to serve it. The entry path is a permissionless query call available to any anonymous or authenticated caller. [10](#0-9) 

### Recommendation
Mirror the ICRC-152 pattern: only include `"ICRC-106"` in the supported standards list when `ledger.index_principal()` returns `Some(_)`. Specifically, change the unconditional push of the ICRC-106 `StandardRecord` to a conditional block:

```rust
if let Some(_) = Access::with_ledger(|ledger| ledger.index_principal()) {
    standards.push(StandardRecord {
        name: "ICRC-106".to_string(),
        url: "https://github.com/dfinity/ICRC/pull/106".to_string(),
    });
}
```

Update the test `test_icrc106_unsupported_if_index_not_set` to assert that ICRC-106 is **not** present in the standards list when no index principal is set, and add a complementary test asserting it **is** present after the index principal is configured via upgrade.

### Proof of Concept
1. Deploy the ICRC-1 ledger with default `InitArgs` (no `index_principal`).
2. Call `icrc10_supported_standards()` as any anonymous principal — the response includes `{ name: "ICRC-106", url: "..." }`.
3. Call `icrc106_get_index_principal()` — the response is `Err(IndexPrincipalNotSet)`.
4. Call `icrc1_metadata()` — the `icrc106:index_principal` key is absent.

Steps 2–4 are directly confirmed by `assert_index_not_set` in the test helper, which asserts `expect_icrc106_supported = true` while simultaneously asserting `icrc106_get_index_principal` returns `Err(Icrc106Error::IndexPrincipalNotSet)` and the metadata key is absent: [11](#0-10)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L778-781)
```rust
        StandardRecord {
            name: "ICRC-106".to_string(),
            url: "https://github.com/dfinity/ICRC/pull/106".to_string(),
        },
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L783-788)
```rust
    if Access::with_ledger(|ledger| ledger.feature_flags().icrc152) {
        standards.push(StandardRecord {
            name: "ICRC-152".to_string(),
            url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-152.md".to_string(),
        });
    }
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1176-1179)
```rust
#[query]
fn icrc10_supported_standards() -> Vec<StandardRecord> {
    supported_standards()
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1181-1187)
```rust
#[query]
fn icrc106_get_index_principal() -> Result<Principal, Icrc106Error> {
    Access::with_ledger(|ledger| match ledger.index_principal() {
        None => Err(Icrc106Error::IndexPrincipalNotSet),
        Some(index_principal) => Ok(index_principal),
    })
}
```

**File:** packages/icrc-ledger-types/src/icrc106/errors.rs (L4-11)
```rust
#[derive(Debug, CandidType, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum Icrc106Error {
    IndexPrincipalNotSet,
    GenericError {
        error_code: Nat,
        description: String,
    },
}
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L185-188)
```rust
            max_memo_length: None,
            feature_flags: None,
            index_principal: None,
        })
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L265-278)
```rust
pub struct InitArgs {
    pub minting_account: Account,
    pub fee_collector_account: Option<Account>,
    pub initial_balances: Vec<(Account, Nat)>,
    pub transfer_fee: Nat,
    pub decimals: Option<u8>,
    pub token_name: String,
    pub token_symbol: String,
    pub metadata: Vec<(String, Value)>,
    pub archive_options: ArchiveOptions,
    pub max_memo_length: Option<u16>,
    pub feature_flags: Option<FeatureFlags>,
    pub index_principal: Option<Principal>,
}
```

**File:** rs/ledger_suite/tests/sm-tests/src/icrc_106.rs (L3-33)
```rust
pub fn test_icrc106_supported_even_if_index_not_set<T, U>(
    ledger_wasm: Vec<u8>,
    encode_ledger_init_args: fn(InitArgs) -> T,
    encode_upgrade_args: fn(Option<Principal>) -> U,
) where
    T: CandidType,
    U: CandidType,
{
    let env = StateMachine::new();
    let ledger_canister_id = env.create_canister(None);
    let ledger_init_args = encode_ledger_init_args(init_args(vec![]));
    env.install_existing_canister(
        ledger_canister_id,
        ledger_wasm.clone(),
        Encode!(&ledger_init_args).unwrap(),
    )
    .expect("should successfully install ledger canister");

    assert_index_not_set(&env, ledger_canister_id, true);

    let args = encode_upgrade_args(None);
    let encoded_upgrade_args = Encode!(&args).unwrap();
    env.upgrade_canister(
        ledger_canister_id,
        ledger_wasm,
        encoded_upgrade_args.clone(),
    )
    .expect("should successfully upgrade ledger canister");

    assert_index_not_set(&env, ledger_canister_id, true);
}
```

**File:** rs/ledger_suite/tests/sm-tests/src/icrc_106.rs (L164-181)
```rust
fn assert_index_not_set(
    env: &StateMachine,
    ledger_canister_id: CanisterId,
    expect_icrc106_supported: bool,
) {
    check_icrc106_support(env, ledger_canister_id, expect_icrc106_supported);
    if expect_icrc106_supported {
        assert_eq!(
            Err(Icrc106Error::IndexPrincipalNotSet),
            icrc106_get_index_principal(env, ledger_canister_id)
        );
    }
    assert_eq!(
        None,
        metadata(env, ledger_canister_id)
            .get(&MetadataKey::parse(MetadataKey::ICRC106_INDEX_PRINCIPAL).unwrap())
    );
}
```

**File:** rs/ledger_suite/icrc1/ledger/tests/tests.rs (L602-609)
```rust
#[test]
fn test_icrc106_unsupported_if_index_not_set() {
    ic_ledger_suite_state_machine_tests::icrc_106::test_icrc106_supported_even_if_index_not_set(
        ledger_wasm(),
        encode_init_args,
        encode_icrc106_upgrade_args,
    );
}
```

**File:** packages/icrc-ledger-types/src/icrc/metadata_key.rs (L97-99)
```rust
    /// The textual representation of the principal of the associated index canister.
    pub const ICRC106_INDEX_PRINCIPAL: &'static str = "icrc106:index_principal";

```
