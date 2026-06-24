### Title
Trapped SNS Tokens in Aborted Decentralization Swap - (File: rs/sns/swap/src/swap.rs)

### Summary
When an SNS decentralization swap is aborted (e.g., minimum participation threshold not reached before the deadline), the SNS tokens preloaded into the swap canister are permanently locked with no on-chain retrieval mechanism. The `finalize_inner` function's abort path returns early after sweeping ICP back to buyers and restoring dapp controllers, but never returns the SNS tokens to the SNS treasury.

### Finding Description

The SNS swap canister is preloaded with SNS tokens before the swap opens (in `LIFECYCLE_PENDING` state). The proto documentation explicitly states this:

> "Step 1 (State PENDING). The swap canister is loaded with the right amount of SNS tokens." [1](#0-0) 

When a swap is aborted, `finalize_inner` executes the following path:

1. `sweep_icp` — returns ICP to buyers ✓
2. `settle_neurons_fund_participation` — handles Neurons' Fund ✓
3. `should_restore_dapp_control()` returns `true` for aborted swaps → restores dapp controllers → **returns early** [2](#0-1) 

The early return at line 1583 means `sweep_sns` is never called. Furthermore, `sweep_sns` itself enforces that SNS tokens can only be distributed in the `COMMITTED` lifecycle: [3](#0-2) 

There is no `retrieve_sns`, `return_sns`, `refund_sns`, or equivalent function anywhere in the swap canister source. The `error_refund_icp` function only handles ICP, not SNS tokens.

The integration test suite explicitly confirms this behavior — when the swap is aborted, the full SNS token balance remains in the swap canister: [4](#0-3) 

The expected `FinalizeSwapResponse` for an aborted swap has `sweep_sns_result: None`, confirming SNS tokens are never swept: [5](#0-4) 

### Impact Explanation

Every failed SNS decentralization swap results in the permanent loss of the SNS tokens allocated for that swap. These tokens are locked in the swap canister indefinitely. The swap canister is a one-time-use canister per the lifecycle design — it cannot be reopened after abort. The SNS treasury loses the full `swap_distribution_sns_e8s` amount with no protocol-level recovery path. Recovery would require an NNS governance proposal to upgrade the swap canister and add a custom withdrawal function, which is an out-of-band administrative action not guaranteed to succeed.

**Impact:** Ledger conservation bug — SNS tokens are minted/allocated but permanently unrecoverable after a swap abort.

### Likelihood Explanation

Any SNS swap that fails to reach its minimum ICP participation threshold before the deadline will be aborted. This is a normal, expected outcome for any unsuccessful SNS launch. The abort is triggered automatically via the canister heartbeat (`run_periodic_tasks` → `try_abort`), and `finalize` is callable by any unprivileged ingress sender. No special privileges are required to trigger the abort path. Every historical and future failed SNS swap is affected.

### Recommendation

In `finalize_inner`, after `sweep_icp` and before the early return at line 1583, add a step to transfer the full SNS token balance held by the swap canister back to the SNS treasury (or a designated fallback account specified in `Init`). This mirrors the `sweep_icp` pattern but in reverse — returning unsold SNS tokens to their origin. Alternatively, add a dedicated `retrieve_sns_tokens` endpoint callable by the SNS root or fallback controllers after finalization of an aborted swap.

### Proof of Concept

1. An SNS is created; the swap canister is loaded with `N` SNS tokens.
2. The swap opens but fails to reach `min_direct_participation_icp_e8s` before `swap_due_timestamp_seconds`.
3. The heartbeat calls `try_abort`, transitioning the swap to `LIFECYCLE_ABORTED`.
4. Any unprivileged caller invokes `finalize` on the swap canister.
5. `finalize_inner` executes: sweeps ICP back to buyers, settles Neurons' Fund, restores dapp controllers, then **returns early** at line 1583.
6. `sweep_sns` is never called. The SNS ledger balance of the swap canister remains at `N` tokens.
7. No public endpoint exists to retrieve these tokens. The SNS treasury has permanently lost `N` SNS tokens. [6](#0-5) [3](#0-2)

### Citations

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L115-117)
```text
// Step 1 (State PENDING). The swap canister is loaded with the right
// amount of SNS tokens. A call to `open` will then transition the
// canister to the OPEN state.
```

**File:** rs/sns/swap/src/swap.rs (L1556-1584)
```rust
        // Transfer the ICP tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Settle the Neurons' Fund participation in the token swap.
        finalize_swap_response.set_settle_neurons_fund_participation_result(
            self.settle_neurons_fund_participation(environment.nns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

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

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L1336-1342)
```rust
        if swap_finalization_status == SwapFinalizationStatus::Aborted {
            // If the swap fails, the SNS swap does not distribute any tokens.
            assert_eq!(swap_canister_balance_sns_e8s, swap_distribution_sns_e8s);
        } else {
            // In a happy scenario, the SNS swap distributes all the tokens.
            assert_eq!(swap_canister_balance_sns_e8s, 0);
        }
```

**File:** rs/nervous_system/integration_tests/src/pocket_ic_helpers.rs (L2927-2940)
```rust
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
