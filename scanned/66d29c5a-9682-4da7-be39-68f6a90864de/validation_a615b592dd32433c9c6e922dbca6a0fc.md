### Title
SNS Swap Tokens Permanently Locked in Swap Canister After Aborted Swap - (File: `rs/sns/swap/src/swap.rs`)

---

### Summary

When an SNS decentralization swap is aborted (fails to reach minimum participation before the deadline), the SNS tokens pre-loaded into the swap canister are never returned to the SNS governance treasury. The `finalize_inner` function for aborted swaps refunds ICP to buyers and restores dapp control, but returns early without ever calling `sweep_sns`. The SNS tokens remain permanently locked in the swap canister's ledger account. Once the swap canister is deleted per the lifecycle (`ABORTED → <DELETED>`), those tokens become irrecoverable.

---

### Finding Description

The SNS decentralization swap lifecycle requires that SNS tokens be pre-loaded into the swap canister's account on the SNS ledger before the swap opens. If the swap is committed, `finalize_inner` calls `sweep_sns` to distribute those tokens to participants as neurons. However, if the swap is aborted, `finalize_inner` takes a different code path: [1](#0-0) 

Specifically, after calling `sweep_icp` (to refund ICP to buyers) and `settle_neurons_fund_participation`, the function checks `should_restore_dapp_control()`. For aborted swaps this returns `true`, so the function restores dapp controllers and **returns early** — `sweep_sns` is never reached: [2](#0-1) 

The `sweep_sns` function itself is guarded to only execute in `Lifecycle::Committed`: [3](#0-2) 

There is no corresponding `sweep_sns_to_governance` or any other mechanism to return the unsold SNS tokens to the SNS governance treasury after an abort. The integration test explicitly confirms this as the observed behavior — after an aborted swap, the full `swap_distribution_sns_e8s` remains in the swap canister's ledger account: [4](#0-3) 

The swap lifecycle diagram documents that the swap canister is deleted after abort: [5](#0-4) 

When the swap canister is deleted, the SNS ledger account it controlled still holds the tokens, but no canister exists to authorize transfers from that account. The tokens are permanently inaccessible.

---

### Impact Explanation

Any SNS whose decentralization swap is aborted loses the entire `swap_distribution_sns_e8s` allocation — the full token supply earmarked for the swap. These tokens cannot be redistributed, burned, or returned to the treasury. For SNS projects with large swap allocations (potentially tens of millions of governance tokens), this represents a permanent, irrecoverable loss of the project's token supply. The SNS governance canister treasury balance is unaffected, but the swap allocation is destroyed, permanently reducing the circulating supply available for decentralization.

---

### Likelihood Explanation

Any SNS swap that fails to reach `min_participants` or `min_icp_e8s` before `swap_due_timestamp_seconds` will be aborted. This is a realistic and common scenario — new SNS projects may fail to attract sufficient participation. The abort path is triggered automatically by the canister heartbeat with no special attacker action required. Any unprivileged principal can observe the swap state and predict an abort. The entry path is fully reachable via the public `finalize` endpoint callable by any principal once the swap is in `Lifecycle::Aborted`. [6](#0-5) 

---

### Recommendation

Add a `sweep_sns_to_governance` step inside `finalize_inner` for the aborted path, executed before the early return, that transfers the remaining SNS token balance from the swap canister's account back to the SNS governance treasury subaccount. This mirrors the existing `sweep_icp` refund logic but targets the SNS ledger and the governance treasury as the destination.

---

### Proof of Concept

1. Deploy an SNS with `swap_distribution_sns_e8s = X` tokens pre-loaded into the swap canister's SNS ledger account.
2. Open the swap with `min_icp_e8s` set to a value that will not be reached.
3. Allow the swap to expire past `swap_due_timestamp_seconds` without sufficient participation; the heartbeat transitions the swap to `Lifecycle::Aborted`.
4. Call `finalize` on the swap canister. Observe that `sweep_icp_result` shows ICP refunded to buyers, `set_dapp_controllers_call_result` shows dapp control restored, and `sweep_sns_result` is `None` (never executed).
5. Query the SNS ledger for the swap canister's account balance: it equals `X` (the full swap allocation), confirmed by the integration test assertion at `rs/nervous_system/integration_tests/tests/sns_lifecycle.rs:1338`.
6. Delete the swap canister (the controller, SNS root, can do this). The SNS ledger account for the now-deleted swap canister still holds `X` tokens, but no principal can authorize a transfer from it. The tokens are permanently locked. [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1500-1512)
```rust
    pub async fn finalize(
        &mut self,
        now_fn: fn(bool) -> u64,
        environment: &mut impl CanisterEnvironment,
    ) -> FinalizeSwapResponse {
        // Acquire the lock or return a FinalizeSwapResponse with an error message.
        if let Err(error_message) = self.lock_finalize_swap() {
            return FinalizeSwapResponse::with_error(error_message);
        }

        // The lock is now acquired and asynchronous calls to finalize are blocked.
        // Perform all subactions.
        let finalize_swap_response = self.finalize_inner(now_fn, environment).await;
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

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L1322-1343)
```rust
    // B. Inspect Swap's balance.
    {
        let swap_canister_balance_sns_e8s = sns::ledger::icrc1_balance_of(
            &pocket_ic,
            sns.ledger.canister_id,
            Account {
                owner: sns.swap.canister_id.0,
                subaccount: None,
            },
        )
        .await
        .0
        .to_u64()
        .unwrap();
        if swap_finalization_status == SwapFinalizationStatus::Aborted {
            // If the swap fails, the SNS swap does not distribute any tokens.
            assert_eq!(swap_canister_balance_sns_e8s, swap_distribution_sns_e8s);
        } else {
            // In a happy scenario, the SNS swap distributes all the tokens.
            assert_eq!(swap_canister_balance_sns_e8s, 0);
        }
    }
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L57-66)
```text
// ```text
//                                                                     sufficient_participation
//                                                                     && (swap_due || icp_target_reached)
// PENDING -------------------> ADOPTED ---------------------> OPEN -----------------------------------------> COMMITTED
//         Swap receives a request        The opening delay      |                                                |
//         from NNS governance to         has elapsed            | not sufficient_participation                   |
//         schedule opening                                      | && (swap_due || icp_target_reached)            |
//                                                               v                                                v
//                                                            ABORTED ---------------------------------------> <DELETED>
// ```
```
