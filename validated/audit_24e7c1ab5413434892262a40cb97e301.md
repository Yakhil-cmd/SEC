### Title
SNS Tokens Permanently Stuck in Swap Canister After Permissionless `finalize_swap` in ABORTED State — (`rs/sns/swap/canister/canister.rs`, `rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS Swap canister's `finalize_swap` endpoint is fully permissionless. When the swap lifecycle reaches `ABORTED`, any unprivileged caller can invoke `finalize_swap`, which sweeps ICP back to buyers but contains no path to return SNS tokens to the SNS treasury. The swap canister exposes no `error_refund_sns` or equivalent recovery function. The SNS tokens loaded into the swap canister before the swap opened are permanently irrecoverable once finalization completes.

---

### Finding Description

**Permissionless entry point.** `finalize_swap` in `rs/sns/swap/canister/canister.rs` carries no caller check:

```rust
#[update]
async fn finalize_swap(_arg: FinalizeSwapRequest) -> FinalizeSwapResponse {
    log!(INFO, "finalize_swap");
    let mut clients = swap().init_or_panic().environment()...;
    swap_mut().finalize(now_fn, &mut clients).await
}
```

Any principal on the Internet Computer can call this endpoint once the swap is in `COMMITTED` or `ABORTED` state. [1](#0-0) 

**ABORTED finalization path never sweeps SNS tokens.** Inside `finalize_inner`, the ABORTED branch executes `sweep_icp` (refunds ICP to buyers), `settle_neurons_fund_participation`, and `restore_dapp_controllers_for_finalize`, then **returns early**. The `sweep_sns` call that distributes SNS tokens is only reached in the `COMMITTED` branch: [2](#0-1) 

```rust
// Transfer the ICP tokens from the Swap canister.
finalize_swap_response.set_sweep_icp_result(self.sweep_icp(...).await);
...
if self.should_restore_dapp_control() {
    // Restore controllers of dapp canisters ...
    finalize_swap_response.set_set_dapp_controllers_result(...);
    return finalize_swap_response;   // <-- early return; sweep_sns never called
}
// sweep_sns only reached in COMMITTED path
finalize_swap_response.set_sweep_sns_result(self.sweep_sns(...).await);
```

**No SNS token recovery function exists.** The swap canister exposes `error_refund_icp` to return ICP that was sent in error, but there is no analogous `error_refund_sns` or any other function that can transfer SNS tokens out of the swap canister back to the SNS treasury after an abort. [3](#0-2) 

**Confirmed by integration test.** The lifecycle integration test explicitly asserts that SNS tokens remain in the swap canister after an abort and are never distributed: [4](#0-3) 

```rust
if swap_finalization_status == SwapFinalizationStatus::Aborted {
    // If the swap fails, the SNS swap does not distribute any tokens.
    assert_eq!(swap_canister_balance_sns_e8s, swap_distribution_sns_e8s);
}
```

---

### Impact Explanation

SNS tokens pre-loaded into the swap canister (the `swap_distribution_sns_e8s` allocation) are permanently locked in the swap canister's ledger account after an aborted swap is finalized. The swap canister has no built-in mechanism to transfer these tokens back to the SNS governance treasury subaccount or to burn them. Because the swap canister's lifecycle is terminal after finalization, and no recovery endpoint exists, the tokens are irrecoverable without an NNS-level canister upgrade — a governance action that may not be timely or guaranteed. This constitutes a **ledger conservation bug**: tokens are neither distributed to participants nor returned to the issuer.

---

### Likelihood Explanation

Any SNS swap that fails to meet its minimum participation threshold will abort. This is a normal, expected outcome for many SNS launches. Once the swap is in `ABORTED` state, any unprivileged user (including a bot or automated script) can call `finalize_swap` to trigger the stuck state. The call requires no special permissions, no tokens, and no prior relationship with the SNS. The likelihood of this occurring for every aborted swap is therefore **high**.

---

### Recommendation

1. Add a `sweep_sns_on_abort` step inside `finalize_inner` for the `ABORTED` lifecycle path that transfers all remaining SNS tokens from the swap canister's account back to the SNS governance treasury subaccount (mirroring the `sweep_icp` refund logic).
2. Alternatively, add an `error_refund_sns` endpoint (analogous to `error_refund_icp`) that allows the SNS treasury or any caller to trigger a return of SNS tokens to the SNS governance canister after finalization.
3. Ensure the `can_finalize` guard or a post-finalization check verifies that the SNS token balance of the swap canister is zero before considering finalization complete.

---

### Proof of Concept

1. An SNS is created and its swap canister is loaded with `N` SNS tokens (PENDING → OPEN transition).
2. The swap opens but fails to reach `min_participants` or `min_icp_e8s` before the deadline.
3. The swap transitions to `ABORTED` (automatically via heartbeat).
4. Eve (any unprivileged principal) calls `finalize_swap({})` on the swap canister.
5. `finalize_inner` executes: `sweep_icp` refunds all ICP to buyers; `restore_dapp_controllers_for_finalize` restores dapp control; the function returns early.
6. `sweep_sns` is never called. The `N` SNS tokens remain in the swap canister's account on the SNS ledger.
7. No function on the swap canister can move these tokens. The SNS treasury has permanently lost `N` SNS tokens. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L150-159)
```rust
#[update]
async fn finalize_swap(_arg: FinalizeSwapRequest) -> FinalizeSwapResponse {
    log!(INFO, "finalize_swap");
    let mut clients = swap()
        .init_or_panic()
        .environment()
        .expect("unable to create canister clients");

    swap_mut().finalize(now_fn, &mut clients).await
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
