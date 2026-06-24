### Title
ICRC-1 Ledger Unconditionally Advertises ICRC-106 Support Even When Index Principal Is Not Set — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

The ICRC-1 ledger's `icrc1_supported_standards` endpoint unconditionally includes `"ICRC-106"` in its response regardless of whether an index principal has been configured. When any caller subsequently invokes `icrc106_get_index_principal`, the ledger returns `Err(IndexPrincipalNotSet)` instead of a valid principal. This is a direct analog to the ERC-4906 finding: the ledger advertises a standard it cannot honor in its current state, misleading every client that relies on the standards list to determine whether index-canister discovery is available.

---

### Finding Description

`supported_standards()` in `rs/ledger_suite/icrc1/ledger/src/main.rs` unconditionally pushes `"ICRC-106"` into the returned vector with no guard on whether the index principal is actually set:

```rust
// rs/ledger_suite/icrc1/ledger/src/main.rs:751-790
#[query(name = "icrc1_supported_standards")]
fn supported_standards() -> Vec<StandardRecord> {
    let mut standards = vec![
        ...
        StandardRecord {
            name: "ICRC-106".to_string(),
            url: "https://github.com/dfinity/ICRC/pull/106".to_string(),
        },
    ];
    // Only ICRC-152 is gated on a feature flag; ICRC-106 is always present.
    if Access::with_ledger(|ledger| ledger.feature_flags().icrc152) { ... }
    standards
}
```

The endpoint that is supposed to deliver the advertised capability returns an error whenever the index principal has not been configured:

```rust
// rs/ledger_suite/icrc1/ledger/src/main.rs:1182-1187
#[query]
fn icrc106_get_index_principal() -> Result<Principal, Icrc106Error> {
    Access::with_ledger(|ledger| match ledger.index_principal() {
        None => Err(Icrc106Error::IndexPrincipalNotSet),
        Some(index_principal) => Ok(index_principal),
    })
}
```

The test suite explicitly encodes and validates this inconsistency as the intended behavior:

```rust
// rs/ledger_suite/tests/sm-tests/src/icrc_106.rs:3-33
pub fn test_icrc106_supported_even_if_index_not_set<...>(...) {
    ...
    assert_index_not_set(&env, ledger_canister_id, true); // ICRC-106 in standards, but IndexPrincipalNotSet
}
```

`assert_index_not_set` with `expect_icrc106_supported = true` asserts both that `"ICRC-106"` appears in `icrc1_supported_standards` **and** that `icrc106_get_index_principal` returns `Err(IndexPrincipalNotSet)` simultaneously.

The `icrc1_metadata` endpoint also omits the `icrc106:index_principal` key in this state, so neither the standards list nor the metadata gives a consistent picture.

---

### Impact Explanation

The ICRC-10 standard (`icrc10_supported_standards`) and ICRC-1 standard (`icrc1_supported_standards`) are the canonical discovery mechanism used by wallets, explorers, aggregators, and inter-canister integrations to determine which capabilities a ledger exposes. Advertising `"ICRC-106"` signals that the ledger can return a trusted index-canister principal. Any client that:

1. Calls `icrc1_supported_standards`, sees `"ICRC-106"`, and proceeds to call `icrc106_get_index_principal` to locate the index canister, will receive `Err(IndexPrincipalNotSet)`.
2. Caches the standards list (a common optimization) and later calls `icrc106_get_index_principal` after the index principal has been set will have stale negative knowledge.

Concretely: wallets that use the standards list to decide whether to show transaction history via the index canister will silently fail to display it; DeFi protocols that verify the index canister before accepting deposits may reject valid ledgers; automated tooling that treats `"ICRC-106"` in the standards list as a guarantee of a resolvable index will malfunction. The `icrc10_supported_standards` alias at line 1177 propagates the same false claim.

---

### Likelihood Explanation

Every ICRC-1 ledger instance that was upgraded to the current version without explicitly passing an `index_principal` in the upgrade args is in this state. The upgrade path documented in `test_upgrade_downgrade_with_mainnet_ledger` shows that upgrading from a mainnet ledger without setting the index principal leaves the ledger advertising ICRC-106 while returning `IndexPrincipalNotSet`. This is the default post-upgrade state for all existing ckBTC, ckETH, ckERC-20, and SNS ledgers that have not yet had their index principal set via a subsequent upgrade. Any unprivileged query caller can trigger the inconsistency with two successive read-only calls.

---

### Recommendation

Gate the inclusion of `"ICRC-106"` in `supported_standards()` on whether the index principal is actually set, mirroring the existing pattern used for `"ICRC-152"`:

```rust
if Access::with_ledger(|ledger| ledger.index_principal().is_some()) {
    standards.push(StandardRecord {
        name: "ICRC-106".to_string(),
        url: "https://github.com/dfinity/ICRC/pull/106".to_string(),
    });
}
```

This ensures that `icrc1_supported_standards` only advertises ICRC-106 when `icrc106_get_index_principal` can return a valid principal, making the two endpoints consistent. The test `test_icrc106_supported_even_if_index_not_set` should be updated to assert that ICRC-106 is **not** present when the index principal is unset.

---

### Proof of Concept

1. Deploy an ICRC-1 ledger (or upgrade an existing one) without providing an `index_principal` in the init/upgrade args.
2. Call `icrc1_supported_standards` (query, no authentication required). Observe `"ICRC-106"` in the response.
3. Call `icrc106_get_index_principal` (query, no authentication required). Observe `Err(variant { IndexPrincipalNotSet })`.
4. Call `icrc1_metadata`. Observe that the `icrc106:index_principal` key is absent.

Steps 2–4 are confirmed by `test_icrc106_supported_even_if_index_not_set` and `assert_index_not_set` in the test suite. The ledger simultaneously claims ICRC-106 compliance and fails to deliver its sole required behavior. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L751-790)
```rust
#[query(name = "icrc1_supported_standards")]
fn supported_standards() -> Vec<StandardRecord> {
    let mut standards = vec![
        StandardRecord {
            name: "ICRC-1".to_string(),
            url: "https://github.com/dfinity/ICRC-1/tree/main/standards/ICRC-1".to_string(),
        },
        StandardRecord {
            name: "ICRC-2".to_string(),
            url: "https://github.com/dfinity/ICRC-1/tree/main/standards/ICRC-2".to_string(),
        },
        StandardRecord {
            name: "ICRC-3".to_string(),
            url: "https://github.com/dfinity/ICRC-1/tree/main/standards/ICRC-3".to_string(),
        },
        StandardRecord {
            name: "ICRC-10".to_string(),
            url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-10/ICRC-10.md".to_string(),
        },
        StandardRecord {
            name: "ICRC-21".to_string(),
            url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-21/ICRC-21.md".to_string(),
        },
        StandardRecord {
            name: "ICRC-103".to_string(),
            url: "https://github.com/dfinity/ICRC/tree/main/ICRCs/ICRC-103".to_string(),
        },
        StandardRecord {
            name: "ICRC-106".to_string(),
            url: "https://github.com/dfinity/ICRC/pull/106".to_string(),
        },
    ];
    if Access::with_ledger(|ledger| ledger.feature_flags().icrc152) {
        standards.push(StandardRecord {
            name: "ICRC-152".to_string(),
            url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-152.md".to_string(),
        });
    }
    standards
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

**File:** rs/ledger_suite/tests/sm-tests/src/icrc_106.rs (L1-33)
```rust
use super::*;

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
