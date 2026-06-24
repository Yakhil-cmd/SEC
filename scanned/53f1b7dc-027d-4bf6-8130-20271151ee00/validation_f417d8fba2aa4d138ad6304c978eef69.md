### Title
ICRC-3 Archive Block-Type Registry Omits ICRC-122 Block Types When ICRC-152 Is Enabled — (`rs/ledger_suite/icrc1/archive/src/main.rs`)

---

### Summary

The ICRC-1 archive canister's `icrc3_supported_block_types` endpoint hardcodes a fixed list of block-type identifiers that permanently excludes `"122mint"` and `"122burn"`. When the ledger has ICRC-152 enabled, it produces blocks with those exact `btype` values and archives them. Any ICRC-3-compliant client that uses `icrc3_supported_block_types` for discovery will never learn that the archive holds ICRC-122 blocks, causing those blocks to be silently skipped during history reconstruction.

---

### Finding Description

The ICRC-3 standard requires every canister that stores blocks to advertise, via `icrc3_supported_block_types`, every block-type identifier it can contain. The archive canister's implementation returns a static list:

`"1burn"`, `"1mint"`, `"1xfer"`, `"2approve"`, `"2xfer"`, `BTYPE_107` [1](#0-0) 

`"122mint"` and `"122burn"` are never included, regardless of whether the paired ledger has ICRC-152 enabled.

The ledger, by contrast, conditionally appends those block types to its own `icrc3_supported_block_types` response when the `icrc152` feature flag is set, and the integration tests explicitly assert their presence: [2](#0-1) 

The block-type constants and their string values are defined in the shared schema package: [3](#0-2) 

When `icrc152_mint` or `icrc152_burn` is called, the ledger records a block whose `btype` field is `"122mint"` / `"122burn"` and eventually archives it: [4](#0-3) [5](#0-4) 

The archive stores those blocks faithfully but never tells callers they exist.

---

### Impact Explanation

An ICRC-3-compliant client (wallet, explorer, chain-fusion bridge, or index canister) that follows the standard discovery flow — call `icrc3_supported_block_types`, then call `icrc3_get_blocks` only for the advertised types — will silently omit every archived ICRC-152 mint and burn. Because the ledger's own `icrc3_supported_block_types` does advertise `"122mint"` / `"122burn"`, the client will process those blocks while they reside on the ledger, but once they are moved to the archive the same client will stop seeing them. The result is a split view of the transaction log: pre-archival blocks are visible, post-archival blocks of the same type are invisible. For any system that reconstructs balances or audits supply from the block log (e.g., a chain-fusion bridge verifying authorized mints), this produces incorrect accounting without any on-chain error signal.

---

### Likelihood Explanation

ICRC-152 is a production feature flag on the ICRC-1 ledger. Any deployment that enables it and accumulates enough transactions to trigger archiving will expose this discrepancy to every ICRC-3-compliant client. The ICRC-3 standard explicitly defines `icrc3_supported_block_types` as the authoritative discovery mechanism, so well-behaved clients are expected to rely on it. The defect is deterministic and reproducible on every archive canister paired with an ICRC-152-enabled ledger.

---

### Recommendation

The archive's `icrc3_supported_block_types` must be made aware of the block types the paired ledger can produce. Two approaches:

1. **Dynamic propagation**: When the ledger archives a batch of blocks, include the set of block types present in that batch in the archive initialization or upgrade argument, and have the archive store and return them.
2. **Static inclusion with feature awareness**: Pass the ledger's feature flags to the archive at spawn time and conditionally include `"122mint"` / `"122burn"` in the archive's `icrc3_supported_block_types` response when ICRC-152 is enabled.

Either way, the archive's advertised block-type set must be a superset of every `btype` value it can store, matching the ICRC-3 requirement.

---

### Proof of Concept

1. Deploy an ICRC-1 ledger with `icrc152: true` in the feature flags.
2. Call `icrc152_mint` enough times to fill the ledger's in-memory block window and trigger archiving.
3. Query `icrc3_supported_block_types` on the **archive** canister — observe that `"122mint"` and `"122burn"` are absent.
4. Query `icrc3_get_blocks` on the archive for the archived range — observe that blocks with `btype = "122mint"` are present and retrievable.
5. A client following the standard discovery flow (step 3 before step 4) would never issue the query in step 4 for those block types, silently missing all archived ICRC-152 mints and burns. [1](#0-0) [3](#0-2) [6](#0-5)

### Citations

**File:** rs/ledger_suite/icrc1/archive/src/main.rs (L324-356)
```rust
#[query]
fn icrc3_supported_block_types() -> Vec<SupportedBlockType> {
    vec![
        SupportedBlockType {
            block_type: "1burn".to_string(),
            url: "https://github.com/dfinity/ICRC-1/blob/main/standards/ICRC-1/README.md"
                .to_string(),
        },
        SupportedBlockType {
            block_type: "1mint".to_string(),
            url: "https://github.com/dfinity/ICRC-1/blob/main/standards/ICRC-1/README.md"
                .to_string(),
        },
        SupportedBlockType {
            block_type: "1xfer".to_string(),
            url: "https://github.com/dfinity/ICRC-1/blob/main/standards/ICRC-1/README.md"
                .to_string(),
        },
        SupportedBlockType {
            block_type: "2approve".to_string(),
            url: "https://github.com/dfinity/ICRC-1/blob/main/standards/ICRC-2/README.md"
                .to_string(),
        },
        SupportedBlockType {
            block_type: "2xfer".to_string(),
            url: "https://github.com/dfinity/ICRC-1/blob/main/standards/ICRC-2/README.md"
                .to_string(),
        },
        SupportedBlockType {
            block_type: BTYPE_107.to_string(),
            url: "https://github.com/dfinity/ICRC/pull/117".to_string(),
        },
    ]
```

**File:** rs/ledger_suite/tests/sm-tests/src/lib.rs (L6594-6644)
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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L951-961)
```rust
        let tx = Transaction {
            operation: Operation::AuthorizedMint {
                to: args.to,
                amount,
                caller: Some(caller),
                mthd: Some(MTHD_152_MINT.to_string()),
                reason: args.reason,
            },
            created_at_time: Some(args.created_at_time),
            memo: None,
        };
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
