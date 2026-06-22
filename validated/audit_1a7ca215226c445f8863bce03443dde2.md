### Title
Replacing `ledger_id` via `UpgradeArg` After Index Has Indexed Blocks Corrupts Index State - (`rs/ledger_suite/icrc1/index-ng/src/main.rs` and `rs/ledger_suite/icp/index/src/main.rs`)

---

### Summary

Both the ICRC-1 `index-ng` canister and the ICP `index` canister expose a `ledger_id: Option<Principal>` field in their `UpgradeArg`. During `post_upgrade`, if this field is `Some(new_principal)`, the canister unconditionally overwrites `state.ledger_id` with the new value — with no guard checking whether the index has already indexed blocks from the original ledger. This is the direct IC analog of the `setLpToken` vulnerability: a privileged setter can replace a critical contract reference after users have already built state against the original reference, breaking the integrity of all previously indexed data.

---

### Finding Description

**ICRC-1 index-ng** (`rs/ledger_suite/icrc1/index-ng`):

The `UpgradeArg` struct exposes `ledger_id: Option<Principal>`:

```rust
pub struct UpgradeArg {
    pub ledger_id: Option<Principal>,
    ...
}
```

In `post_upgrade`, the replacement is unconditional:

```rust
mutate_state(|state| {
    if let Some(new_value) = ledger_id {
        state.ledger_id = new_value;   // no guard: index may already have N blocks
    }
    ...
});
```

The index's stable memory (`BLOCKS`, `ACCOUNT_BLOCK_IDS`, `ACCOUNT_DATA`) is **not cleared** when `ledger_id` is replaced. After the upgrade, the timer-driven `build_index` loop resumes by calling `get_blocks_from_ledger(next_id)` where `next_id = blocks.len()` — the count of blocks already indexed from the **old** ledger. It then queries the **new** ledger starting at that offset, appending new-ledger blocks directly after old-ledger blocks in the same log. All balance deltas and account-to-block-id mappings computed from old-ledger blocks remain in stable memory and are never invalidated.

The same pattern exists in the ICP index canister:

```rust
mutate_state(|state| {
    if let Some(new_value) = ledger_id {
        state.ledger_id = new_value;
    }
    ...
});
```

---

### Impact Explanation

After `ledger_id` is replaced mid-operation:

1. **Corrupted block log**: The `BLOCKS` stable log contains a splice of old-ledger blocks followed by new-ledger blocks starting at an arbitrary offset. The `chain_length` reported by `get_blocks` is meaningless.
2. **Wrong balances**: `icrc1_balance_of` on the index returns balances computed from a mix of two unrelated ledgers' transaction histories.
3. **Wrong transaction history**: `get_account_transactions` returns transactions from both ledgers interleaved, with incorrect block indices.
4. **Permanent corruption**: Unlike the Solidity case where the old `lpToken` can be restored to recover funds, the IC index's stable memory block log is append-only (`StableLog`). Blocks appended from the new ledger cannot be removed. The only recovery is a full canister reinstall, which wipes all indexed state.

Any wallet, DeFi application, or bridge that relies on the index for balance or transaction queries will receive incorrect data, potentially leading to incorrect user decisions or exploitable discrepancies between the ledger's true state and the index's reported state.

---

### Likelihood Explanation

The `ledger_id` field in `UpgradeArg` is a documented, intentional feature — it was introduced specifically to support the SNS legacy-index-to-index-ng migration path. This means:

- Every SNS deployment's index canister (controlled by SNS root, which is controlled by SNS governance) can have its `ledger_id` changed via a governance upgrade proposal.
- Any developer who deploys their own ICRC-1 ledger suite and controls the index canister directly can trigger this accidentally or intentionally.
- The NNS controls the ckBTC, ckETH, and ckERC-20 index canisters; an NNS proposal with a wrong `ledger_id` in the upgrade args would corrupt those indexes.

There is no on-chain guard, no warning in the interface, and no check that `blocks.len() == 0` before allowing the replacement.

---

### Recommendation

1. **Reject `ledger_id` changes if the index has already indexed blocks**: In `post_upgrade`, before applying a new `ledger_id`, assert that `with_blocks(|b| b.len()) == 0`. If blocks have already been indexed, trap with a descriptive error.
2. **Alternatively, only allow `ledger_id` to be set once**: Track whether `ledger_id` has been explicitly set (separate from the default `Principal::management_canister()`) and reject subsequent changes.
3. **Document the invariant**: If the intent is to allow `ledger_id` changes only during the legacy-to-ng migration (where the old state is read from stable memory and the block log is empty), enforce this programmatically rather than relying on operator discipline.

---

### Proof of Concept

1. Deploy an ICRC-1 ledger `L1` and its index-ng canister `I`.
2. Perform 100 transfers on `L1`; wait for `I` to sync (`num_blocks_synced = 100`).
3. Deploy a second, unrelated ICRC-1 ledger `L2` with a completely different set of accounts and balances.
4. Upgrade `I` with `UpgradeArg { ledger_id: Some(L2.principal()), ... }`.
5. After the next timer tick, `I` calls `L2.get_blocks(start=100, length=2000)`. If `L2` has fewer than 100 blocks, the index stalls. If `L2` has more than 100 blocks, it appends `L2`'s blocks 100..N to the log that already contains `L1`'s blocks 0..99.
6. Query `I.icrc1_balance_of(account_from_L1)`: returns a balance computed from `L1`'s history only (no `L2` transfers for that account), while `I.icrc1_balance_of(account_from_L2)` returns a balance computed from `L2`'s history starting at block 100, missing all earlier `L2` transfers.
7. The index now permanently serves incorrect data for both ledgers.

**Relevant code locations:**

- `UpgradeArg.ledger_id` field: [1](#0-0) 
- Unconditional replacement in `post_upgrade` (index-ng): [2](#0-1) 
- `build_index` resumes from `blocks.len()` after upgrade: [3](#0-2) 
- Unconditional replacement in `post_upgrade` (ICP index): [4](#0-3) 
- ICP index `UpgradeArg.ledger_id` field: [5](#0-4) 
- `build_index` resumes from `blocks.len()` (ICP index): [6](#0-5)

### Citations

**File:** rs/ledger_suite/icrc1/index-ng/src/lib.rs (L28-36)
```rust
pub struct UpgradeArg {
    pub ledger_id: Option<Principal>,
    #[deprecated(
        note = "This field is deprecated and will be removed in a future version. Please use min_retrieve_blocks_from_ledger_interval_seconds and max_retrieve_blocks_from_ledger_interval_seconds instead."
    )]
    pub retrieve_blocks_from_ledger_interval_seconds: Option<u64>,
    pub min_retrieve_blocks_from_ledger_interval_seconds: Option<u64>,
    pub max_retrieve_blocks_from_ledger_interval_seconds: Option<u64>,
}
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L427-430)
```rust
            mutate_state(|state| {
                if let Some(new_value) = ledger_id {
                    state.ledger_id = new_value;
                }
```

**File:** rs/ledger_suite/icrc1/index-ng/src/main.rs (L747-750)
```rust
async fn fetch_blocks_via_get_blocks() -> Result<u64, SyncError> {
    let mut num_indexed = 0;
    let next_id = with_blocks(|blocks| blocks.len());
    let res = get_blocks_from_ledger(next_id).await?;
```

**File:** rs/ledger_suite/icp/index/src/main.rs (L292-295)
```rust
            mutate_state(|state| {
                if let Some(new_value) = ledger_id {
                    state.ledger_id = new_value;
                }
```

**File:** rs/ledger_suite/icp/index/src/main.rs (L371-373)
```rust
    let next_txid = with_blocks(|blocks| blocks.len());
    log!(P0, "[build_index]: next transaction id is {:?}", next_txid);
    let res = match get_blocks_from_ledger(next_txid).await {
```

**File:** rs/ledger_suite/icp/index/src/lib.rs (L22-25)
```rust
pub struct UpgradeArg {
    pub ledger_id: Option<Principal>,
    pub retrieve_blocks_from_ledger_interval_seconds: Option<u64>,
}
```
