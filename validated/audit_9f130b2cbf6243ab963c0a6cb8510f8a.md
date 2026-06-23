### Title
SNS Swap Canister Permanently Locks Pre-Loaded SNS Tokens on Abort - (`rs/sns/swap/src/swap.rs`)

### Summary

The SNS Swap canister is a prefunded single-price auction: SNS tokens are transferred to the swap canister's account on the SNS ledger before the swap opens. When a swap reaches `LIFECYCLE_ABORTED`, the `finalize_inner` function returns early after sweeping ICP back to buyers and restoring dapp controllers — it never transfers the pre-loaded SNS tokens back to the SNS governance canister. Those tokens remain permanently locked in the swap canister's SNS ledger account with no recovery path.

### Finding Description

The SNS Swap canister is pre-funded with SNS tokens during the `PENDING` state before the swap opens:

> "Step 1 (State PENDING). The swap canister is loaded with the right amount of SNS tokens." [1](#0-0) 

When a swap aborts (minimum participation not reached), `finalize_inner` executes the following path:

1. `sweep_icp` — returns ICP to buyers ✓
2. `settle_neurons_fund_participation` — handles Neurons' Fund maturity ✓
3. `should_restore_dapp_control()` returns `true` because lifecycle is `Aborted`
4. `restore_dapp_controllers_for_finalize` is called, then the function **returns early**
5. `sweep_sns` is **never called** — SNS tokens are never returned [2](#0-1) 

The guard that causes the early return: [3](#0-2) 

`sweep_sns` only distributes SNS tokens in the `COMMITTED` path: [4](#0-3) 

In the `COMMITTED` case, the single-price clearing mechanism distributes **all** SNS tokens to buyers (price = total_sns_tokens / total_icp_raised), so no tokens are left over. In the `ABORTED` case, **all** pre-loaded SNS tokens remain in the swap canister's SNS ledger account with no mechanism to recover them.

There is no function in the swap canister API that transfers SNS tokens back to governance after an abort. `error_refund_icp` only handles ICP. The SNS governance canister does not control the swap canister's SNS ledger account and cannot forcibly transfer from it (ICRC-1 has no admin-transfer capability). [5](#0-4) 

### Impact Explanation

Every SNS swap that reaches `LIFECYCLE_ABORTED` permanently destroys the entire pre-loaded SNS token supply that was allocated for the swap. These tokens are locked in the swap canister's SNS ledger account. If the swap canister is subsequently deleted (as the lifecycle diagram indicates: `ABORTED → <DELETED>`), the tokens become permanently inaccessible. This is a ledger conservation violation: tokens are minted/transferred into the swap but can never be recovered. [6](#0-5) 

### Likelihood Explanation

Swap abortion is a normal, expected protocol outcome — it occurs whenever `min_participants` or `min_icp_e8s` is not reached before the swap deadline. Any SNS project whose decentralization swap fails to attract sufficient participation will lose its entire token allocation. This is reachable by any unprivileged user simply by not participating (or participating below the minimum threshold), and it requires no special access.

### Recommendation

In `finalize_inner`, before returning early on the `ABORTED` path, add a step to transfer the remaining SNS token balance from the swap canister's SNS ledger account back to the SNS governance canister's treasury account. This mirrors how `sweep_icp` returns ICP to buyers on abort.

```rust
if self.should_restore_dapp_control() {
    // NEW: Return pre-loaded SNS tokens to SNS governance treasury
    self.sweep_sns_to_governance(now_fn, environment.sns_ledger()).await;

    finalize_swap_response.set_set_dapp_controllers_result(
        self.restore_dapp_controllers_for_finalize(environment.sns_root_mut()).await,
    );
    return finalize_swap_response;
}
```

### Proof of Concept

1. An SNS is created and its swap canister is pre-loaded with `N` SNS tokens in `PENDING` state.
2. The swap opens (`OPEN` state) but fails to reach `min_icp_e8s` or `min_participants` before the deadline.
3. `try_abort` is called, transitioning the swap to `ABORTED`.
4. `finalize` is called. `finalize_inner` executes:
   - `sweep_icp` returns all ICP to buyers.
   - `should_restore_dapp_control()` returns `true`.
   - `restore_dapp_controllers_for_finalize` is called.
   - Function returns early — `sweep_sns` is never invoked.
5. The swap canister's SNS ledger account still holds `N` SNS tokens.
6. No function exists to recover these tokens. If the swap canister is deleted, the tokens are permanently lost. [7](#0-6)

### Citations

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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L114-117)
```text
//
// Step 1 (State PENDING). The swap canister is loaded with the right
// amount of SNS tokens. A call to `open` will then transition the
// canister to the OPEN state.
```

**File:** rs/sns/swap/src/swap.rs (L1348-1350)
```rust
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
