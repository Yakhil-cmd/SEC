### Title
ICRC-122 Block Standard Omitted from `icrc1_supported_standards` / `icrc10_supported_standards` Despite Ledger Producing ICRC-122 Blocks — (`rs/ledger_suite/icrc1/ledger/src/main.rs`)

---

### Summary

When the `icrc152` feature flag is enabled, the ICRC-1 ledger canister produces blocks whose `btype` field is `"122mint"` or `"122burn"` — identifiers defined by the **ICRC-122** block-format standard. The `icrc3_supported_block_types` endpoint correctly advertises these block types with ICRC-122 URLs. However, the `icrc1_supported_standards` / `icrc10_supported_standards` endpoint never includes `"ICRC-122"` in its response; it only adds `"ICRC-152"`. Any ICRC-10-compliant client that queries `icrc1_supported_standards` or `icrc10_supported_standards` to discover whether the ledger produces ICRC-122 blocks will receive a false negative, causing integration failures.

---

### Finding Description

The `supported_standards()` function, exposed as both `icrc1_supported_standards` and (via `icrc10_supported_standards`) as the ICRC-10 discovery endpoint, unconditionally lists ICRC-1, ICRC-2, ICRC-3, ICRC-10, ICRC-21, ICRC-103, and ICRC-106. When the `icrc152` feature flag is set, it appends `"ICRC-152"` only: [1](#0-0) 

The `icrc3_supported_block_types` endpoint, however, adds block types `"122mint"` and `"122burn"` with URLs pointing explicitly to the ICRC-122 specification: [2](#0-1) 

The schema module makes the two-standard split explicit: `BTYPE_122_MINT = "122mint"` and `BTYPE_122_BURN = "122burn"` are **ICRC-122 block-type identifiers**, while `MTHD_152_MINT = "152mint"` and `MTHD_152_BURN = "152burn"` are **ICRC-152 method discriminators**: [3](#0-2) 

The `icrc152_mint` and `icrc152_burn` handlers produce transactions with `Operation::AuthorizedMint` / `Operation::AuthorizedBurn` that are serialised into blocks carrying `btype: "122mint"` / `btype: "122burn"`, confirmed by integration tests: [4](#0-3) [5](#0-4) 

So the ledger **implements** ICRC-122 block format and **advertises** it through `icrc3_supported_block_types`, but **never declares** `"ICRC-122"` in `icrc1_supported_standards` or `icrc10_supported_standards`. The two discovery surfaces are inconsistent.

The `icrc10_supported_standards` endpoint simply re-exports `supported_standards()`: [6](#0-5) 

The `.did` file confirms both endpoints are part of the public canister interface: [7](#0-6) 

---

### Impact Explanation

ICRC-10 is the canonical standard-discovery mechanism on the Internet Computer. Clients — including other canisters, indexers, wallets, and the Rosetta API — are expected to call `icrc1_supported_standards` or `icrc10_supported_standards` to determine which block formats a ledger produces before attempting to parse or validate blocks. A client that checks for `"ICRC-122"` in the supported-standards list will conclude the ledger does not produce ICRC-122 blocks, even though it does. Consequences include:

- Block parsers that gate ICRC-122 block handling on the standard declaration will silently skip or misclassify `"122mint"` / `"122burn"` blocks, corrupting derived state (balances, audit trails).
- The index canister already calls `icrc1_supported_standards` to discover ledger capabilities: [8](#0-7) 

Any index or aggregator that gates ICRC-122 block indexing on this call will fail to index authorized mint/burn blocks, producing incorrect balance histories.

---

### Likelihood Explanation

The `icrc1_supported_standards` and `icrc10_supported_standards` endpoints are query calls reachable by any unprivileged caller with no authentication required. The mismatch is present in every deployment where the `icrc152` feature flag is enabled. Any ICRC-10-compliant integration that follows the standard discovery pattern will be affected. The existing test suite (`test_icrc152_supported_standards`) only checks that `"ICRC-152"` appears and that `"122mint"` / `"122burn"` appear in `icrc3_supported_block_types`; it never asserts that `"ICRC-122"` appears in `icrc1_supported_standards`, so the gap is not caught: [9](#0-8) 

---

### Recommendation

Add `"ICRC-122"` to the `supported_standards()` return value whenever the `icrc152` feature flag is enabled (since ICRC-152 endpoints produce ICRC-122 format blocks):

```rust
if Access::with_ledger(|ledger| ledger.feature_flags().icrc152) {
    standards.push(StandardRecord {
        name: "ICRC-122".to_string(),
        url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-122.md".to_string(),
    });
    standards.push(StandardRecord {
        name: "ICRC-152".to_string(),
        url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-152.md".to_string(),
    });
}
```

Update `test_icrc152_supported_standards` to assert that `"ICRC-122"` is present in `icrc1_supported_standards` when the flag is enabled.

---

### Proof of Concept

1. Deploy the ICRC-1 ledger with `feature_flags: Some(FeatureFlags { icrc2: true, icrc152: true })`.
2. Call `icrc1_supported_standards` (query, no authentication). Observe the response contains `"ICRC-152"` but **not** `"ICRC-122"`.
3. Call `icrc3_supported_block_types` (query). Observe `"122mint"` and `"122burn"` are listed with `url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-122.md"`.
4. Call `icrc152_mint` as a controller. Retrieve the resulting block via `icrc3_get_blocks`. Observe `btype: "122mint"` — an ICRC-122 block type.
5. A client that gates ICRC-122 block parsing on `icrc1_supported_standards` containing `"ICRC-122"` will skip step 4's block entirely, producing incorrect derived state. [10](#0-9) [2](#0-1) [3](#0-2)

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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L1158-1167)
```rust
    if Access::with_ledger(|ledger| ledger.feature_flags().icrc152) {
        types.push(SupportedBlockType {
            block_type: "122mint".to_string(),
            url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-122.md".to_string(),
        });
        types.push(SupportedBlockType {
            block_type: "122burn".to_string(),
            url: "https://github.com/dfinity/ICRC/blob/main/ICRCs/ICRC-122.md".to_string(),
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

**File:** packages/icrc-ledger-types/src/icrc122/schema.rs (L9-15)
```rust
/// Block type identifiers (ICRC-122 standard)
pub const BTYPE_122_MINT: &str = "122mint";
pub const BTYPE_122_BURN: &str = "122burn";

/// Method discriminators (ICRC-152 endpoint standard)
pub const MTHD_152_MINT: &str = "152mint";
pub const MTHD_152_BURN: &str = "152burn";
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L6492-6496)
```rust
    assert_eq!(
        btype,
        Some(ICRC3Value::Text("122mint".to_string())),
        "mint block should have btype 122mint"
    );
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L6530-6534)
```rust
    assert_eq!(
        btype,
        Some(ICRC3Value::Text("122burn".to_string())),
        "burn block should have btype 122burn"
    );
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L6594-6645)
```rust
pub fn test_icrc152_supported_standards<T>(
    ledger_wasm: Vec<u8>,
    encode_init_args: fn(InitArgs) -> T,
) where
    T: CandidType,
{
    // With icrc152 disabled (default setup)
    let (env, canister_id_disabled) = setup(ledger_wasm.clone(), encode_init_args, vec![]);
    let standards_disabled: Vec<String> = supported_standards(&env, canister_id_disabled)
        .into_iter()
        .map(|s| s.name)
        .collect();
    assert!(
        !standards_disabled.contains(&"ICRC-152".to_string()),
        "ICRC-152 should NOT be in supported_standards when disabled"
    );
    let block_types_disabled: Vec<String> = supported_block_types(&env, canister_id_disabled)
        .into_iter()
        .map(|bt| bt.block_type)
        .collect();
    assert!(
        !block_types_disabled.contains(&"122mint".to_string()),
        "122mint should NOT be in supported_block_types when disabled"
    );
    assert!(
        !block_types_disabled.contains(&"122burn".to_string()),
        "122burn should NOT be in supported_block_types when disabled"
    );

    // With icrc152 enabled
    let (env, canister_id_enabled) = setup_icrc152(ledger_wasm, encode_init_args, vec![]);
    let standards_enabled: Vec<String> = supported_standards(&env, canister_id_enabled)
        .into_iter()
        .map(|s| s.name)
        .collect();
    assert!(
        standards_enabled.contains(&"ICRC-152".to_string()),
        "ICRC-152 should be in supported_standards when enabled, got: {standards_enabled:?}"
    );
    let block_types_enabled: Vec<String> = supported_block_types(&env, canister_id_enabled)
        .into_iter()
        .map(|bt| bt.block_type)
        .collect();
    assert!(
        block_types_enabled.contains(&"122mint".to_string()),
        "122mint should be in supported_block_types when enabled, got: {block_types_enabled:?}"
    );
    assert!(
        block_types_enabled.contains(&"122burn".to_string()),
        "122burn should be in supported_block_types when enabled, got: {block_types_enabled:?}"
    );
}
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L620-632)
```text
  icrc1_supported_standards : () -> (vec StandardRecord) query;

  icrc2_approve : (ApproveArgs) -> (ApproveResult);
  icrc2_allowance : (AllowanceArgs) -> (Allowance) query;
  icrc2_transfer_from : (TransferFromArgs) -> (TransferFromResult);

  icrc3_get_archives : (GetArchivesArgs) -> (GetArchivesResult) query;
  icrc3_get_tip_certificate : () -> (opt ICRC3DataCertificate) query;
  icrc3_get_blocks : (vec GetBlocksArgs) -> (GetBlocksResult) query;
  icrc3_supported_block_types : () -> (vec record { block_type : text; url : text }) query;

  icrc21_canister_call_consent_message : (icrc21_consent_message_request) -> (icrc21_consent_message_response);
  icrc10_supported_standards : () -> (vec record { name : text; url : text }) query;
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L523-557)
```rust
async fn get_supported_standards_from_ledger() -> Vec<String> {
    let ledger_id = with_state(|state| state.ledger_id);
    log!(
        P1,
        "[get_supported_standards_from_ledger]: making the call..."
    );
    let res: Result<Vec<StandardRecord>, String> =
        match Call::unbounded_wait(ledger_id, "icrc1_supported_standards").await {
            Ok(response) => response
                .candid::<Vec<StandardRecord>>()
                .map_err(|err| err.to_string()),
            Err(err) => Err(err.to_string()),
        };
    match res {
        Ok(res) => {
            let supported_standard_names = res.into_iter().map(|s| s.name).collect::<Vec<_>>();
            log!(
                P1,
                "[get_supported_standards_from_ledger]: ledger {} supports {:?}",
                ledger_id,
                supported_standard_names,
            );
            supported_standard_names
        }
        Err(err) => {
            // log the error but do not propagate it
            log!(
                P0,
                "[get_supported_standards_from_ledger]: failed to call icrc1_supported_standards on ledger {}: {}",
                ledger_id,
                err,
            );
            vec![]
        }
    }
```
