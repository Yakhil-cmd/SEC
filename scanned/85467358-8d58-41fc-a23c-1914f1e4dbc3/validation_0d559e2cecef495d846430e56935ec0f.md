### Title
SNS Tokens Permanently Locked in Swap Canister When Swap Is Aborted — (`File: rs/sns/swap/src/swap.rs`)

---

### Summary

When an SNS decentralization swap is aborted (e.g., due to insufficient participation or zero buyers), the SNS tokens pre-loaded into the swap canister are never returned to the SNS treasury. The `finalize_inner` function returns early in the ABORTED path without ever invoking `sweep_sns`, leaving the SNS tokens permanently locked in the swap canister with no recovery mechanism.

---

### Finding Description

The SNS Swap canister implements a single-price auction lifecycle. Before a swap opens, SNS tokens are transferred to the swap canister's account on the SNS ledger (PENDING state). If the swap is ABORTED — because the due date passes without meeting `min_participants`, `min_icp_e8s`, or other conditions — `finalize_inner` is called to settle the swap.

In `finalize_inner`, the ABORTED path is:

1. `sweep_icp` — refunds ICP to buyers
2. `settle_neurons_fund_participation` — settles NF
3. `should_restore_dapp_control()` returns `true` (because lifecycle == ABORTED)
4. `restore_dapp_controllers_for_finalize` — restores dapp controllers
5. **Returns early at line 1583** — execution stops here [1](#0-0) 

The `sweep_sns` call (which transfers SNS tokens out of the swap canister) is only reached in the COMMITTED path, after the early return guard: [2](#0-1) 

Furthermore, `sweep_sns` itself explicitly rejects any call when the lifecycle is not COMMITTED: [3](#0-2) 

There is no code path in the ABORTED lifecycle that transfers SNS tokens back to the SNS governance canister or any treasury account. The SNS tokens deposited before the swap opened remain in the swap canister's ledger account indefinitely.

The proto definition confirms that `sweep_sns_result` is expected to be `None` in the aborted finalization response, meaning the design intentionally omits SNS token recovery: [4](#0-3) 

---

### Impact Explanation

**Vulnerability class:** Ledger conservation bug / resource accounting bug — tokens locked with no recovery path.

When a swap is ABORTED (including the zero-buyers case), all SNS tokens pre-loaded into the swap canister are permanently locked. The SNS governance canister loses those tokens from its effective supply. Since the swap canister is eventually deleted after finalization, the tokens become unrecoverable. This directly mirrors the SpiceAuction issue: the `sweep_sns` function acts as the analog of `recoverToken`, and it is gated exclusively to the COMMITTED lifecycle, leaving no recovery path for the ABORTED case.

---

### Likelihood Explanation

ABORTED swaps are a documented, expected lifecycle outcome — any SNS swap that fails to attract sufficient participation transitions to ABORTED. This is not a rare edge case; it is a first-class lifecycle state with its own finalization path. The zero-buyers scenario (a direct analog to "auction ends with no bids") is also reachable: if no participant calls `refresh_buyer_tokens` before the swap due date, the swap aborts with `buyers` empty and all SNS tokens locked.

---

### Recommendation

In `finalize_inner`, before returning early in the ABORTED path, add a step to transfer the SNS tokens held by the swap canister back to the SNS governance treasury (or a designated recovery account). This is analogous to how `sweep_icp` returns ICP to buyers in the ABORTED path. A new `sweep_sns_to_treasury` function should:

- Be callable only in ABORTED state
- Transfer the full SNS token balance held by the swap canister back to the SNS governance canister's main account
- Be idempotent (safe to retry on failure) [5](#0-4) 

---

### Proof of Concept

1. An SNS is created and SNS tokens are transferred to the swap canister (PENDING state).
2. The swap is opened via NNS governance proposal.
3. No participants call `refresh_buyer_tokens` before the swap due date (zero buyers / no bids).
4. The heartbeat triggers `try_abort`, setting lifecycle to ABORTED.
5. Anyone calls `finalize` (public endpoint).
6. `finalize_inner` executes: `sweep_icp` (no-op, zero buyers), `settle_neurons_fund_participation`, `restore_dapp_controllers_for_finalize`, then **returns early**.
7. `sweep_sns` is never called. SNS tokens remain in the swap canister's SNS ledger account.
8. The swap canister is eventually deleted. SNS tokens are permanently lost.

The test at `rs/sns/swap/src/swap.rs` line 4636 confirms a zero-buyer abort is reachable: [6](#0-5) 

The aborted finalization test confirms `sweep_sns_result` is always `None` in the ABORTED path, with no SNS token recovery: [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1572-1584)
```rust
        if self.should_restore_dapp_control() {
            // Restore controllers of dapp canisters to their original
            // owners (i.e. self.init.fallback_controller_principal_ids).
            finalize_swap_response.set_set_dapp_controllers_result(
                self.restore_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );

            // In the case of returning control of the dapp(s) to the fallback
            // controllers, finalize() need not do any more work, so always return
            // and end execution.
            return finalize_swap_response;
        }
```

**File:** rs/sns/swap/src/swap.rs (L1593-1598)
```rust
        // Transfer the SNS tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_sns_result(self.sweep_sns(now_fn, environment.sns_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }
```

**File:** rs/sns/swap/src/swap.rs (L2165-2178)
```rust
    pub async fn sweep_sns(
        &mut self,
        now_fn: fn(bool) -> u64,
        sns_ledger: &dyn ICRC1Ledger,
    ) -> SweepResult {
        if self.lifecycle() != Lifecycle::Committed {
            log!(
                ERROR,
                "Halting sweep_sns(). SNS Tokens cannot be distributed if \
                Lifecycle is not COMMITTED. Current Lifecycle: {:?}",
                self.lifecycle()
            );
            return SweepResult::new_with_global_failures(1);
        }
```

**File:** rs/sns/swap/src/swap.rs (L4636-4669)
```rust
    #[test]
    fn test_try_commit_or_abort_insufficient_participation_with_no_time_remaining() {
        let sale_duration = 100;
        let time_remaining = 0;
        let now = sale_duration - time_remaining;
        let buyers = BTreeMap::new();
        let mut swap = SwapBuilder::new()
            .with_lifecycle(Lifecycle::Open)
            .with_buyers(buyers)
            .with_swap_start_due(None, Some(sale_duration))
            .with_min_participants(1)
            .with_min_max_participant_icp(10, 20)
            .with_min_max_direct_participation(10, 20)
            .build();

        // test try_commit
        {
            let mut swap = swap.clone();
            let result = swap.try_commit(now);
            // swap should not commit because there is no time remaining and we
            // have not reached the minimum number of participants

            assert!(!result);
            assert_eq!(swap.lifecycle, Lifecycle::Open as i32);
        }
        // test try_abort
        {
            let result = swap.try_abort(now);
            // swap should abort because there is no time remaining and we
            // have not reached the minimum number of participants

            assert!(result);
            assert_eq!(swap.lifecycle, Lifecycle::Aborted as i32);
        }
```

**File:** rs/nervous_system/integration_tests/src/pocket_ic_helpers.rs (L2917-2940)
```rust
        pub fn is_auto_finalization_status_aborted_or_err(
            auto_finalization_status: &GetAutoFinalizationStatusResponse,
        ) -> Result<bool, String> {
            let Some(auto_finalize_swap_response) =
                validate_auto_finalization_status(auto_finalization_status)?
            else {
                return Ok(false);
            };
            // Otherwise, either `auto_finalization_status` matches the expected structure of it does not
            // indicate that the swap has been aborted yet.
            Ok(matches!(
                auto_finalize_swap_response,
                FinalizeSwapResponse {
                    sweep_icp_result: Some(_),
                    set_dapp_controllers_call_result: Some(_),
                    settle_neurons_fund_participation_result: Some(_),
                    create_sns_neuron_recipes_result: None,
                    sweep_sns_result: None,
                    claim_neuron_result: None,
                    set_mode_call_result: None,
                    settle_community_fund_participation_result: None,
                    error_message: None,
                }
            ))
```
