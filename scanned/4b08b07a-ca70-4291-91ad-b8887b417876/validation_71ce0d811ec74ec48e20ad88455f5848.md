### Title
SNS Swap Canister Permanently Strands SNS Tokens on Aborted Swap — (File: rs/sns/swap/src/swap.rs)

### Summary
When an SNS decentralization swap is aborted due to insufficient participation, the SNS tokens pre-loaded into the swap canister are never returned to the SNS governance treasury. The `finalize_inner` function handles the aborted lifecycle by refunding ICP to buyers and restoring dapp controllers, then returns early — it never sweeps SNS tokens back. No recovery path exists. This is the direct IC analog of the BribeRewarder stranded-funds class: tokens deposited for distribution become permanently locked when the distribution event fails.

### Finding Description

Before a swap opens, the SNS token allocation (`swap_distribution_sns_e8s`) is transferred to the swap canister's account on the SNS ledger. The swap canister is the sole authorized signer for that account.

When the swap is aborted (`Lifecycle::Aborted`), `finalize_inner` executes the following steps and then returns early:

1. `sweep_icp` — refunds ICP to buyers
2. `settle_neurons_fund_participation` — refunds Neurons' Fund maturity
3. `restore_dapp_controllers_for_finalize` — restores dapp control to fallback controllers
4. **Returns immediately** — no SNS token sweep is performed [1](#0-0) 

The SNS tokens remain in the swap canister's ledger account. Because the swap canister is the only principal that can authorize transfers from that account, and because the lifecycle diagram explicitly shows `ABORTED → <DELETED>` as the terminal path, once the swap canister is deleted those tokens become permanently inaccessible on the SNS ledger. [2](#0-1) 

The integration test suite explicitly confirms this behavior — it asserts that after an aborted swap the full `swap_distribution_sns_e8s` balance remains in the swap canister, with no expectation of recovery: [3](#0-2) 

The committed path, by contrast, does call `sweep_sns` to distribute tokens to participants: [4](#0-3) 

There is no analogous sweep-back call for the aborted path.

### Impact Explanation

The entire SNS token swap allocation — which can represent millions of governance tokens — is permanently stranded in the swap canister's ledger account with no mechanism for the SNS project or any other party to recover them. Once the swap canister is deleted, the tokens are inaccessible forever. This is a **ledger conservation bug**: tokens minted and transferred for a specific purpose are destroyed without serving that purpose and without being returned to any treasury.

### Likelihood Explanation

Any SNS swap that fails to reach `min_participants` or `min_icp_e8s` before the deadline transitions to `Aborted`. This is a realistic and documented lifecycle path — the protocol explicitly supports it. New or low-visibility SNS projects are particularly susceptible. No attacker action is required; normal under-participation triggers the loss automatically.

### Recommendation

In `finalize_inner`, when the lifecycle is `Aborted`, add a step to transfer the remaining SNS token balance from the swap canister's account back to the SNS governance treasury account before returning. This mirrors the existing `sweep_icp` pattern used to refund ICP to buyers. The transfer should be idempotent (guarded by a flag, as with other sweep operations) so that retried `finalize` calls do not double-transfer.

### Proof of Concept

1. An SNS is created and its swap canister is loaded with `N` SNS tokens.
2. The swap opens but does not reach `min_participants` before the deadline.
3. `try_abort` transitions the swap to `Aborted`.
4. `finalize` is called. `finalize_inner` runs `sweep_icp` (ICP returned to buyers), `settle_neurons_fund_participation` (NF maturity refunded), `restore_dapp_controllers_for_finalize` (dapp control restored), then returns early.
5. Query the SNS ledger: `icrc1_balance_of(swap_canister_account)` returns `N` (unchanged).
6. No principal can now move those tokens: the swap canister holds no further callable state that would authorize a transfer, and the SNS root/governance canisters have no authority over the swap canister's ledger account.
7. When the swap canister is eventually deleted, the `N` SNS tokens are permanently inaccessible. [5](#0-4) [6](#0-5)

### Citations

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
