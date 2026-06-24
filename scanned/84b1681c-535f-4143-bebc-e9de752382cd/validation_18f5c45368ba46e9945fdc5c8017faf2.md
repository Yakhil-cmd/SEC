### Title
SNS Swap Canister Lacks SNS Token Withdrawal Functionality on Aborted Swap - (File: rs/sns/swap/src/swap.rs)

### Summary
The SNS Swap canister receives SNS tokens before the swap opens (PENDING state) but provides no mechanism to return those tokens to the SNS governance treasury when the swap is ABORTED. The `finalize_inner` function returns early on abort after restoring dapp controllers, skipping any SNS token recovery. `sweep_sns` explicitly rejects non-COMMITTED lifecycle. There is no `error_refund_sns` counterpart to `error_refund_icp`. SNS tokens are permanently locked in the swap canister.

### Finding Description

The SNS Swap canister is pre-loaded with SNS tokens in the PENDING state before the swap opens. [1](#0-0) 

When a swap is ABORTED (minimum participation not reached by due date), `finalize_inner` handles the abort path by calling `sweep_icp` (to return ICP to buyers) and `settle_neurons_fund_participation`, then calls `restore_dapp_controllers_for_finalize` and **returns early** — before any SNS token recovery step: [2](#0-1) 

The only SNS token distribution function, `sweep_sns`, explicitly guards against non-COMMITTED lifecycle: [3](#0-2) 

There is no `error_refund_sns` function — only `error_refund_icp` exists in the canister interface: [4](#0-3) 

An integration test explicitly confirms this behavior — after abort, the full `swap_distribution_sns_e8s` balance remains locked in the swap canister with no recovery path: [5](#0-4) 

The swap proto documentation states the canister should be deletable only "when all tokens registered with the swap canister have been disbursed to their rightful owners," but the aborted path leaves SNS tokens permanently stranded. [6](#0-5) 

### Impact Explanation

**Impact: High.** SNS tokens allocated for the decentralization swap — potentially a large fraction of the total SNS token supply — are permanently locked in the swap canister when the swap is aborted. The SNS governance treasury cannot recover these tokens. The swap canister cannot be cleanly deleted. The SNS project suffers permanent, irrecoverable token loss proportional to the swap allocation.

### Likelihood Explanation

**Likelihood: Medium.** SNS swaps abort when minimum participation thresholds (`min_participants`, `min_icp_e8s`) are not met by the due date. This is a realistic and documented lifecycle outcome. Any SNS swap that fails to attract sufficient participation triggers this loss. No privileged action is required — the abort transition happens automatically on the canister heartbeat. [7](#0-6) 

### Recommendation

Implement an SNS token recovery function analogous to `error_refund_icp` — e.g., `return_sns_tokens_to_governance` — callable after the swap reaches ABORTED state. This function should transfer the remaining SNS token balance from the swap canister back to the SNS governance treasury account. Alternatively, extend `finalize_inner`'s abort path to include a `sweep_sns_back` step that transfers unsold SNS tokens back to the SNS governance canister before returning.

### Proof of Concept

1. An SNS is created and the swap canister is pre-loaded with `N` SNS tokens (e.g., 30% of total supply).
2. The swap opens (OPEN state) but fails to reach `min_participants` or `min_icp_e8s` by `swap_due_timestamp_seconds`.
3. The swap transitions to ABORTED automatically on heartbeat.
4. Any caller invokes `finalize_swap`. `finalize_inner` runs `sweep_icp` (ICP returned to buyers), `settle_neurons_fund_participation` (NF maturity restored), `restore_dapp_controllers_for_finalize` (dapp control returned), then **returns early**.
5. `sweep_sns` is never called (it would reject with `Lifecycle is not COMMITTED`). No `error_refund_sns` exists.
6. Query the SNS ledger balance of the swap canister: it equals the original `swap_distribution_sns_e8s`. These tokens are permanently locked. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L56-66)
```text
//
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L100-117)
```text
// The next step is to provide SNS tokens for the swap. This normally
// happens when the canister is in the PENDING state, and the amount
// is validated in the call to `open`.
//
// The request to open the swap has to originate from the NNS governance
// canister. The request specifies the parameters of the swap, i.e., the
// date/time at which the token swap will take place, the minimal number
// of participants, the minimum number of base tokens (ICP) of each
// participant, as well as the minimum and maximum number (reserve and
// target) of base tokens (ICP) of the swap.
//
// Step 0. The canister is created, specifying the initialization
// parameters, which are henceforth fixed for the lifetime of the
// canister.
//
// Step 1 (State PENDING). The swap canister is loaded with the right
// amount of SNS tokens. A call to `open` will then transition the
// canister to the OPEN state.
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L142-150)
```text
// Step 3b. (State ABORTED). If the parameters of the swap have not
// been satisfied before the due date/time, the swap is aborted and
// the ICP tokens transferred back to their respective owners. The
// swap can also be aborted early if it is determined that the
// swap cannot possibly succeed, e.g., because the ICP ceiling has
// been reached and the minimum number of participants has not been.
//
// The `swap` canister can be deleted when all tokens registered with the
// `swap` canister have been disbursed to their rightful owners.
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

**File:** rs/sns/swap/canister/canister.rs (L161-167)
```rust
#[update]
async fn error_refund_icp(request: ErrorRefundIcpRequest) -> ErrorRefundIcpResponse {
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    swap()
        .error_refund_icp(this_canister_id(), &request, &icp_ledger)
        .await
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
