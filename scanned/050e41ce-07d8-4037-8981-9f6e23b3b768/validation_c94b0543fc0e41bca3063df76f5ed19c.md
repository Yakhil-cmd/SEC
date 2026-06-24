### Title
SNS Tokens Permanently Locked in Swap Canister on Aborted Swap - (`File: rs/sns/swap/src/swap.rs`)

### Summary
When an SNS decentralization swap is aborted, the `finalize_inner` function in the SNS Swap canister returns early after refunding ICP to buyers and restoring dapp controllers, without ever transferring the pre-loaded SNS tokens back to the SNS treasury. These tokens remain locked in the swap canister's ledger account indefinitely, and are permanently lost when the swap canister is eventually deleted.

### Finding Description
The SNS Swap canister is pre-loaded with SNS tokens in the `PENDING` state before the swap opens. When a swap is aborted (lifecycle transitions to `ABORTED`), the `finalize_inner` function handles cleanup: [1](#0-0) 

`should_restore_dapp_control()` returns `true` exclusively when the lifecycle is `ABORTED`: [2](#0-1) 

The function returns early at line 1583 after restoring dapp controllers, skipping the `sweep_sns` call entirely. The `sweep_sns` function itself also enforces this by explicitly rejecting any non-`COMMITTED` lifecycle: [3](#0-2) 

The lifecycle diagram in the protocol definition confirms the swap canister is eventually deleted after abort: [4](#0-3) 

The protocol documentation states: *"The `swap` canister can be deleted when all tokens registered with the `swap` canister have been disbursed to their rightful owners."* However, in the aborted path, the SNS tokens are never disbursed. When the swap canister is deleted, the SNS tokens remain in the swap canister's account on the SNS ledger with no controller able to move them. [5](#0-4) 

### Impact Explanation
**Ledger conservation bug.** SNS tokens pre-loaded into the swap canister are permanently locked and effectively burned when a swap is aborted. The SNS treasury (SNS governance canister) loses the full amount of tokens that were allocated for the swap. For a real SNS deployment, this can represent a significant fraction of the total token supply. The tokens are irrecoverable once the swap canister is deleted, as no principal retains the ability to transfer them from the swap canister's ledger subaccount.

### Likelihood Explanation
Any SNS decentralization swap that fails to meet its minimum participation requirements will trigger this path. This is a realistic and common scenario — swaps can fail due to insufficient community interest, market conditions, or timing. The abort path is exercised in normal protocol operation without any adversarial action required. Any user who participates in a swap that subsequently aborts will observe this behavior.

### Recommendation
In `finalize_inner`, before returning early on the aborted path, add a step to transfer the SNS tokens held by the swap canister back to the SNS governance canister (treasury). This requires a new function analogous to `sweep_icp` but operating on the SNS ledger in the aborted state. The `sweep_sns` function should either be extended to support the aborted path (returning tokens to treasury rather than distributing to buyers), or a new `return_sns_tokens_to_treasury` function should be added and called before the early return at line 1583.

Additionally, the protocol documentation should explicitly state what happens to SNS tokens in the aborted path, and the canister deletion precondition ("all tokens disbursed") should be enforced programmatically.

### Proof of Concept
1. An SNS is deployed and its swap canister is pre-loaded with `N` SNS tokens (e.g., 10% of total supply).
2. The swap opens and runs until the due date, but fails to reach `min_participants` or `min_icp_e8s`.
3. The swap transitions to `ABORTED` via `try_abort`.
4. `finalize` is called (or auto-finalize triggers).
5. `finalize_inner` executes: `sweep_icp` refunds ICP to buyers, `settle_neurons_fund_participation` refunds Neurons' Fund maturity, `restore_dapp_controllers_for_finalize` restores dapp control, then **returns early** at line 1583.
6. `sweep_sns` is never called. The `N` SNS tokens remain in the swap canister's account on the SNS ledger.
7. The swap canister is eventually deleted per the lifecycle diagram (`ABORTED → <DELETED>`).
8. The `N` SNS tokens are now in an account whose controlling canister no longer exists. They are permanently inaccessible — a direct ledger conservation loss for the SNS. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1347-1350)
```rust
    /// The lifecycle MUST be set to Aborted via the commit method.
    pub fn should_restore_dapp_control(&self) -> bool {
        self.lifecycle() == Lifecycle::Aborted
    }
```

**File:** rs/sns/swap/src/swap.rs (L1544-1584)
```rust
    pub async fn finalize_inner(
        &mut self,
        now_fn: fn(bool) -> u64,
        environment: &mut impl CanisterEnvironment,
    ) -> FinalizeSwapResponse {
        let mut finalize_swap_response = FinalizeSwapResponse::default();

        if let Err(e) = self.can_finalize() {
            finalize_swap_response.set_error_message(e);
            return finalize_swap_response;
        }

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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L149-151)
```text
// The `swap` canister can be deleted when all tokens registered with the
// `swap` canister have been disbursed to their rightful owners.
//
```
