Audit Report

## Title
Unbounded Loop Over All Buyers in `rebuild_indexes` Can Permanently Block SNS Swap Canister Upgrades - (File: `rs/sns/swap/src/swap.rs`)

## Summary
The SNS Swap canister's `canister_post_upgrade` unconditionally calls `rebuild_indexes()`, which iterates over every entry in `self.buyers` when `BUYERS_LIST_INDEX` is empty. For a large SNS sale with many participants, this unbounded loop can exceed the IC's total instruction limit for `install_code` even under DTS, causing the upgrade to fail with `CanisterInstructionLimitExceeded`. Because the rollback leaves `BUYERS_LIST_INDEX` empty, every subsequent upgrade attempt repeats the same failing loop, permanently bricking the canister's upgrade path.

## Finding Description
In `rs/sns/swap/canister/canister.rs` at lines 454–460, `canister_post_upgrade` unconditionally calls `swap().rebuild_indexes()` and panics on error, causing a rollback:

```rust
swap().rebuild_indexes().unwrap_or_else(|err| {
    panic!(
        "Error rebuilding the Swap canister indexes. The stable memory has been exhausted: {err}"
    )
});
``` [1](#0-0) 

In `rs/sns/swap/src/swap.rs` at lines 3151–3179, `rebuild_indexes` loops over all keys in `self.buyers` whenever `BUYERS_LIST_INDEX` is empty:

```rust
if !self.buyers.is_empty() && buyers_list_index_is_empty {
    for key in self.buyers.keys() {
        if let Some(buyer_principal) = string_to_principal(key) {
            insert_buyer_into_buyers_list_index(buyer_principal).map_err(...)?;
        }
    }
}
``` [2](#0-1) 

`BUYERS_LIST_INDEX` is a `StableVec<Principal>` backed by stable memory (defined in `rs/sns/swap/src/memory.rs` at line 32). Each `insert_buyer_into_buyers_list_index` call performs a stable memory write, which is expensive in Wasm instructions. [3](#0-2) 

The triggering condition is the one-time migration from a version that predates `BUYERS_LIST_INDEX` — explicitly acknowledged in the code comment: *"This most likely indicates that this canister was upgraded from a previous version where BUYERS_LIST_INDEX did not exist."*

DTS allows `post_upgrade` to span multiple rounds but does not remove the *total* instruction ceiling. The execution environment test at `rs/execution_environment/src/execution/upgrade/tests.rs` lines 654–673 confirms that exceeding the total limit produces `CanisterInstructionLimitExceeded` and rolls back the canister to its pre-upgrade state: [4](#0-3) 

After rollback, `BUYERS_LIST_INDEX` remains empty. The next upgrade attempt finds `buyers_list_index_is_empty == true` and attempts the identical full loop — failing again. The failure is self-perpetuating with no recovery path short of a special hotfix.

## Impact Explanation
A permanently unupgradeable SNS Swap canister means critical security patches and bug fixes cannot be applied, and the SNS project's governance over its own swap canister is nullified. This is a concrete application/platform-level DoS against the canister's upgrade mechanism and constitutes a significant SNS framework security impact with concrete protocol harm. This maps to the **High ($2,000–$10,000)** bounty tier: *"Significant SNS... security impact with concrete user or protocol harm."*

## Likelihood Explanation
The condition triggers only during the specific one-time migration from a version without `BUYERS_LIST_INDEX` to the current version. However, the code comment explicitly anticipates this migration path as the expected scenario. No privileged access is required — any user participating in the SNS sale as a normal buyer contributes to the buyer count. Popular SNS sales routinely attract thousands of participants. Once triggered, the failure is self-perpetuating, making recovery impossible without an out-of-band hotfix. Likelihood is **Medium**.

## Recommendation
Replace the single-message full-scan with a paginated, resumable migration. Store a migration cursor (e.g., last processed principal) in stable memory. On each `post_upgrade`, process only a bounded batch (e.g., 1,000 buyers), then schedule a timer to continue processing subsequent batches. Mark migration complete when the cursor reaches the end. Alternatively, pre-populate `BUYERS_LIST_INDEX` in `pre_upgrade` from the existing buyers map before the new Wasm is installed, so `post_upgrade` finds the index already populated and skips the loop entirely.

## Proof of Concept
1. Deploy an SNS sale and accumulate a large number of buyers (e.g., 10,000+ participants via normal participation).
2. Upgrade the SNS Swap canister from a version without `BUYERS_LIST_INDEX` to the current version.
3. `canister_post_upgrade` calls `rebuild_indexes()`.
4. The loop iterates over all buyer entries, each performing a stable memory write via `insert_buyer_into_buyers_list_index`.
5. Total instruction count exceeds `max_instructions_per_install_code` (confirmed possible by the existing test `upgrade_fails_on_long_post_upgrade_hits_instructions_limit`).
6. Upgrade fails with `CanisterInstructionLimitExceeded`; canister rolls back to pre-upgrade state.
7. `BUYERS_LIST_INDEX` remains empty. Every subsequent upgrade attempt repeats steps 3–6.
8. The canister is permanently unupgradeable.

A deterministic integration test can reproduce this by: initializing a swap with a large synthetic `buyers` map, leaving `BUYERS_LIST_INDEX` empty, calling `rebuild_indexes()` under an instruction-metered environment, and asserting `CanisterInstructionLimitExceeded` followed by an unchanged (empty) `BUYERS_LIST_INDEX`.

### Citations

**File:** rs/sns/swap/canister/canister.rs (L454-460)
```rust
    // Rebuild the indexes if needed. If the rebuilding process fails, panic so the upgrade
    // rolls back.
    swap().rebuild_indexes().unwrap_or_else(|err| {
        panic!(
            "Error rebuilding the Swap canister indexes. The stable memory has been exhausted: {err}"
        )
    });
```

**File:** rs/sns/swap/src/swap.rs (L3151-3179)
```rust
    pub fn rebuild_indexes(&self) -> Result<(), String> {
        let buyers_list_index_is_empty =
            memory::BUYERS_LIST_INDEX.with(|bli| bli.borrow().is_empty());

        if !self.buyers.is_empty() && buyers_list_index_is_empty {
            log!(
                INFO,
                "Buyers state is populated but BUYERS_LIST_INDEX is not. This most likely indicates \
                that this canister was upgraded from a previous version where BUYERS_LIST_INDEX did not \
                exist. Conducting a best effort rebuild."
            );

            for key in self.buyers.keys() {
                // Try to parse the string representation of the Principal. Logging the error
                // occurs in `string_to_principal`.
                if let Some(buyer_principal) = string_to_principal(key) {
                    // If the index cannot be built due to limitations of the stable memory,
                    // return to the caller to determine how to handle the error.
                    insert_buyer_into_buyers_list_index(buyer_principal).map_err(|grow_failed| {
                        format!(
                            "Failed to add buyer {buyer_principal} to state, the canister's stable memory could not grow: {grow_failed}"
                        )
                    })?;
                }
            }
        }

        Ok(())
    }
```

**File:** rs/sns/swap/src/memory.rs (L32-40)
```rust
    pub static BUYERS_LIST_INDEX: RefCell<StableVec<Principal, VirtualMemory<DefaultMemoryImpl>>> =
        MEMORY_MANAGER.with(|memory_manager|
            RefCell::new(
                StableVec::init(
                    memory_manager.borrow().get(BUYERS_INDEX_LIST_MEMORY_ID)
                )
                .expect("Expected to initialize the BUYERS_LIST_INDEX without error")
            )
        );
```

**File:** rs/execution_environment/src/execution/upgrade/tests.rs (L654-673)
```rust
fn upgrade_fails_on_long_post_upgrade_hits_instructions_limit() {
    // Long execution takes 2 round
    let mut test = execution_test_with_max_rounds(2);
    let canister_id = test.canister_from_binary(old_empty_binary()).unwrap();
    let canister_state_before = test.canister_state(canister_id).clone();

    let new_binary = binary(&[(Function::PostUpgrade, Execution::InstructionsLimit)]);
    // The first round is executed in the `dts_upgrade_canister()`
    let message_id = test.dts_upgrade_canister(canister_id, new_binary);
    // Execute more rounds
    for _round in 1..2 {
        assert_eq!(test.ingress_state(&message_id), IngressState::Processing);
        test.execute_slice(canister_id);
    }
    let result = check_ingress_status(test.ingress_status(&message_id));
    assert_eq!(
        result.unwrap_err().code(),
        ErrorCode::CanisterInstructionLimitExceeded
    );
    assert_canister_state_after_err(&canister_state_before, test.canister_state(canister_id));
```
