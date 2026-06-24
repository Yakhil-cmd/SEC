### Title
SNS Tokens Permanently Locked in Swap Canister Account on Aborted Swap - (`File: rs/sns/swap/src/swap.rs`)

### Summary
When an SNS decentralization swap is aborted, the SNS tokens that were pre-loaded into the swap canister's account on the SNS ledger are never returned to the SNS governance/treasury. The `finalize_inner` function returns early after restoring dapp controllers without sweeping SNS tokens back, causing permanent fund loss.

### Finding Description

The SNS swap lifecycle requires that SNS tokens be deposited into the swap canister's account on the SNS ledger before the swap opens (PENDING state). The proto documentation states: *"The swap canister can be deleted when all tokens registered with the swap canister have been disbursed to their rightful owners."*

However, in `finalize_inner`, when the swap is in the `Lifecycle::Aborted` state, the function:

1. Calls `sweep_icp` to return ICP to buyers ✓
2. Calls `settle_neurons_fund_participation` to refund Neurons' Fund maturity ✓
3. Detects `should_restore_dapp_control() == true` (which is true when `lifecycle == Aborted`) and returns **early** after restoring dapp controllers ✓
4. **Never sweeps SNS tokens back to the SNS governance/treasury** ✗ [1](#0-0) 

The early return at line 1583 exits `finalize_inner` entirely, skipping the `sweep_sns` call that only exists in the COMMITTED path: [2](#0-1) 

The `should_restore_dapp_control` function confirms it only triggers on `Aborted`: [3](#0-2) 

There is no `sweep_sns_tokens_back` or equivalent function anywhere in the swap canister. A grep for `unsold`, `remaining.*sns`, `return_sns_tokens`, and `sweep_sns_tokens_back` returns zero matches across all swap source files.



The lifecycle diagram in the proto explicitly shows `ABORTED -> <DELETED>`, meaning the swap canister is eventually deleted — permanently destroying the locked SNS tokens: [4](#0-3) 

### Impact Explanation

**Impact: High.** All SNS tokens pre-loaded into the swap canister's account (`sns_token_e8s`) are permanently lost when a swap is aborted. These tokens are owned by the SNS project and represent real economic value. Since the swap canister is eventually deleted after finalization, the tokens cannot be recovered. This is a direct ledger conservation violation: tokens enter the swap canister's SNS ledger account but have no exit path in the ABORTED lifecycle. [5](#0-4) 

### Likelihood Explanation

**Likelihood: High.** Swap abortion is a normal, expected outcome whenever the minimum participation threshold (`min_icp_e8s` or `min_participants`) is not reached before the swap deadline. This is not an edge case — it is a documented lifecycle state. Every aborted SNS swap triggers this loss. Any unprivileged user can contribute to causing an abort simply by not participating (or participating below the minimum), and the loss occurs automatically upon `finalize` being called. [6](#0-5) 

### Recommendation

Add a `sweep_sns_tokens_back` step in the ABORTED finalization path that transfers the full SNS token balance held by the swap canister back to the SNS governance canister's treasury account (or the SNS governance distribution subaccount). This should be inserted in `finalize_inner` before the early return in the `should_restore_dapp_control()` branch:

```rust
if self.should_restore_dapp_control() {
    // NEW: Return SNS tokens to SNS governance treasury
    finalize_swap_response.set_sweep_sns_tokens_back_result(
        self.sweep_sns_tokens_back(environment.sns_ledger()).await
    );
    // Restore controllers ...
    finalize_swap_response.set_set_dapp_controllers_result(
        self.restore_dapp_controllers_for_finalize(environment.sns_root_mut()).await,
    );
    return finalize_swap_response;
}
``` [1](#0-0) 

### Proof of Concept

1. Deploy an SNS with `sns_token_e8s = 1_000_000_000` and `min_icp_e8s = 500_000_000`.
2. Pre-load the swap canister with 1,000,000,000 SNS tokens (PENDING state).
3. Open the swap.
4. Have participants contribute only 100,000,000 ICP (below minimum).
5. Allow the swap deadline to pass → swap transitions to `Lifecycle::Aborted`.
6. Call `finalize_swap`.
7. Observe: `sweep_icp` returns ICP to buyers; dapp controllers are restored.
8. Query the swap canister's SNS ledger account balance → still holds 1,000,000,000 SNS tokens.
9. No function exists to recover these tokens.
10. After the swap canister is deleted, the 1,000,000,000 SNS tokens are permanently lost. [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1348-1350)
```rust
    pub fn should_restore_dapp_control(&self) -> bool {
        self.lifecycle() == Lifecycle::Aborted
    }
```

**File:** rs/sns/swap/src/swap.rs (L1544-1624)
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

        // Create the SnsNeuronRecipes based on the contribution of direct and NF participants
        finalize_swap_response
            .set_create_sns_neuron_recipes_result(self.create_sns_neuron_recipes());
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Transfer the SNS tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_sns_result(self.sweep_sns(now_fn, environment.sns_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Once SNS tokens have been distributed to the correct accounts, claim
        // them as neurons on behalf of the Swap participants.
        finalize_swap_response.set_claim_neuron_result(
            self.claim_swap_neurons(environment.sns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        finalize_swap_response.set_set_mode_call_result(
            Self::set_sns_governance_to_normal_mode(environment.sns_governance_mut()).await,
        );

        // The following step is non-critical, so we'll do it after we set
        // governance to normal mode, but only if there were no errors.
        if !finalize_swap_response.has_error_message() {
            finalize_swap_response.set_set_dapp_controllers_result(
                self.take_sole_control_of_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );
        }

        finalize_swap_response
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L61-66)
```text
//         Swap receives a request        The opening delay      |                                                |
//         from NNS governance to         has elapsed            | not sufficient_participation                   |
//         schedule opening                                      | && (swap_due || icp_target_reached)            |
//                                                               v                                                v
//                                                            ABORTED ---------------------------------------> <DELETED>
// ```
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L130-148)
```text
// automatically (on the canister heartbeat) when the necessary
// conditions are fulfilled.
//
// Step 3a. (State COMMITTED). Tokens are allocated to participants at
// a single clearing price, i.e., the number of SNS tokens being
// offered divided by the total number of ICP tokens contributed to
// the swap. In this state, a call to `finalize` will create SNS
// neurons for each participant and transfer ICP to the SNS governance
// canister. The call to `finalize` does not happen automatically
// (i.e., on the canister heartbeat) so that there is a caller to
// respond to with potential errors.
//
// Step 3b. (State ABORTED). If the parameters of the swap have not
// been satisfied before the due date/time, the swap is aborted and
// the ICP tokens transferred back to their respective owners. The
// swap can also be aborted early if it is determined that the
// swap cannot possibly succeed, e.g., because the ICP ceiling has
// been reached and the minimum number of participants has not been.
//
```
