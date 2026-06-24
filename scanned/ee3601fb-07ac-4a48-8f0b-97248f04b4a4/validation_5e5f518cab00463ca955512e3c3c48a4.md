### Title
SNS Swap Canister Has No Recovery Mechanism for Stuck SNS Tokens After Aborted Swap - (`File: rs/sns/swap/src/swap.rs`)

### Summary

The SNS Swap canister exposes `error_refund_icp` to rescue ICP tokens that become stuck in buyer subaccounts, but provides no equivalent mechanism to recover SNS tokens that remain in the swap canister's main account when a swap is aborted.

### Finding Description

Before a swap opens, SNS tokens are pre-loaded into the swap canister's main account (`sns_token_e8s`). The `finalize_inner` function handles both the COMMITTED and ABORTED lifecycle paths.

In the ABORTED path, `finalize_inner` calls `sweep_icp` to return ICP to buyers, then calls `restore_dapp_controllers_for_finalize` and **returns early** — `sweep_sns` is never called: [1](#0-0) 

The `sweep_sns` function itself enforces that it only runs in the `Committed` lifecycle: [2](#0-1) 

The existing `error_refund_icp` function only handles ICP tokens from buyer subaccounts: [3](#0-2) 

There is no `error_refund_sns` or equivalent method in the swap canister's public interface. The SNS tokens deposited into the swap canister's main account before the swap opened have no on-chain recovery path after an abort.

### Impact Explanation

**Ledger conservation bug / cycles-resource accounting bug.** When a swap is aborted (fails to meet minimum participation), all SNS tokens pre-loaded into the swap canister are permanently locked. The only recovery path is a governance-approved canister upgrade to add a withdrawal method — a heavyweight, time-consuming process. Until such an upgrade is executed, the SNS project's token supply is effectively reduced by the amount deposited into the swap canister.

### Likelihood Explanation

SNS swaps regularly fail to meet minimum participation thresholds and are aborted. Every aborted SNS swap that pre-loaded tokens into the swap canister triggers this condition. The entry path requires no privileged access: any user can observe the aborted swap state and confirm that `finalize` was called, leaving SNS tokens stranded. The `FinalizeSwapResponse` proto even includes a `sweep_sns_result` field that will be `None` in the aborted case, confirming no SNS token sweep occurred. [4](#0-3) 

### Recommendation

Add an `error_refund_sns` method (analogous to `error_refund_icp`) that, after the swap is in the `Aborted` or `Committed` (fully finalized) state, allows the SNS governance canister (or any caller, since the tokens belong to the SNS project) to recover any remaining SNS token balance from the swap canister's main account back to the SNS governance canister. Alternatively, call a `return_sns_tokens_to_governance` step inside `finalize_inner` on the ABORTED path before returning.

### Proof of Concept

1. An SNS project initializes a swap, pre-loading `N` SNS tokens into the swap canister.
2. The swap opens but does not reach `min_participants` or `min_direct_participation_icp_e8s` before `swap_due_timestamp_seconds`.
3. `finalize` is called. `finalize_inner` executes `sweep_icp` (returning ICP to buyers), then hits the `should_restore_dapp_control()` branch and returns early.
4. `sweep_sns` is never called. The `N` SNS tokens remain in the swap canister's main account.
5. Calling `error_refund_icp` with the SNS governance principal returns an error because it only queries ICP ledger subaccounts, not the SNS ledger.
6. No other public method on the swap canister can transfer SNS tokens out. [5](#0-4) [6](#0-5)

### Citations

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

**File:** rs/sns/swap/src/swap.rs (L1925-1936)
```rust
    pub async fn error_refund_icp(
        &self,
        self_canister_id: CanisterId,
        request: &ErrorRefundIcpRequest,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> ErrorRefundIcpResponse {
        // Fail if the request is premature.
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L862-866)
```text
message FinalizeSwapResponse {
  SweepResult sweep_icp_result = 1;

  SweepResult sweep_sns_result = 2;

```
