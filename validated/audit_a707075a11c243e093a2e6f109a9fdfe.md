### Title
SNS Swap `finalize_inner` ABORTED path: `restore_dapp_controllers` permanently blocked when `sweep_icp` has unresolvable failures - (File: `rs/sns/swap/src/swap.rs`)

---

### Summary
In the SNS Swap canister's `finalize_inner` function, the ABORTED finalization path requires `sweep_icp` (ICP refunds to buyers) to fully succeed before `restore_dapp_controllers` is called. If `sweep_icp` reports any `failure` or `invalid` entries, finalization halts and dapp controllers are never restored to the original developers, leaving the dapp permanently under SNS Root control.

---

### Finding Description

`finalize_inner` in `rs/sns/swap/src/swap.rs` implements a strict sequential execution: each step must fully succeed before the next step runs. In the ABORTED path:

1. `sweep_icp` is called to refund ICP to buyers
2. If `sweep_icp` is not a "successful sweep" (any `failure` or `invalid` entries), `finalize_inner` returns early with an error
3. `restore_dapp_controllers` is never reached [1](#0-0) 

The `set_sweep_icp_result` method sets an error message if `!sweep_icp_result.is_successful_sweep()`: [2](#0-1) 

And `finalize_inner` checks `has_error_message()` after each step to decide whether to halt: [3](#0-2) 

This means that if even one buyer's ICP refund fails (e.g., due to ICP ledger temporary unavailability, or a buyer state with amount below the transfer fee), the `should_restore_dapp_control()` branch at line ~1572 is never reached, and `restore_dapp_controllers_for_finalize` is never called. [4](#0-3) 

The `finalize_swap` canister endpoint is a public `#[update]` callable by any principal: [5](#0-4) 

The test suite explicitly documents that `invalid` buyer entries (amount below transfer fee) cause `sweep_icp` to be considered unsuccessful, halting finalization: [6](#0-5) 

---

### Impact Explanation

Dapp canisters remain under SNS Root control after a swap abortion. The original dapp developers cannot regain control of their dapp. If the blocking condition is permanent — a buyer state with amount permanently below the transfer fee, or a permanently unavailable ICP ledger — `restore_dapp_controllers` can never be called. This is a direct analog to `Vault.blacklistProtocol` being blocked: the emergency recovery action (restoring dapp control to fallback controllers) is blocked by a potentially-failing sub-operation (ICP transfer via `sweep_icp`).

---

### Likelihood Explanation

The ICP ledger can be temporarily unavailable due to subnet issues or maintenance. More critically, if a buyer's committed ICP amount is less than the transfer fee — which the code acknowledges as a possible state even if unlikely in production — `sweep_icp` will always report `invalid` entries, permanently blocking `restore_dapp_controllers`. Any unprivileged user can call `finalize_swap` (it is a public `#[update]` endpoint with no access control), making this reachable without any privileged access. The `finalize_swap_in_progress` lock is released after each failed call, so the blocked state persists across retries whenever the root cause is permanent. [7](#0-6) 

---

### Recommendation

Decouple `restore_dapp_controllers` from `sweep_icp` success in the ABORTED path. The dapp controller restoration should be attempted regardless of whether ICP refunds have fully completed. This mirrors the recommendation in the original report: provide a mechanism to allow the critical state change (restoring dapp control) to proceed independently of the potentially-failing operation (ICP transfer). Concretely, `should_restore_dapp_control()` should be checked and acted upon before or independently of `sweep_icp` error propagation in the ABORTED lifecycle.

---

### Proof of Concept

1. A swap reaches `LIFECYCLE_ABORTED` state (e.g., minimum participation not reached before deadline).
2. One buyer has a committed ICP amount just below the transfer fee (or the ICP ledger is temporarily unavailable).
3. Any unprivileged user calls `finalize_swap`.
4. `sweep_icp` reports `invalid` (or `failure`) entries → `is_successful_sweep()` returns `false`.
5. `set_sweep_icp_result` sets `error_message` → `has_error_message()` returns `true`.
6. `finalize_inner` returns early before reaching the `should_restore_dapp_control()` check.
7. `restore_dapp_controllers_for_finalize` is never called.
8. Dapp canisters remain under SNS Root control indefinitely.
9. Original dapp developers cannot regain control of their dapp.

This is a direct analog to `Vault.blacklistProtocol`: an emergency/recovery function (`finalize_swap` in ABORTED state) is blocked from completing its critical action (restoring dapp control) because it first requires a potentially-failing operation (ICP transfer via `sweep_icp`) to fully succeed. [8](#0-7)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1505-1533)
```rust
        // Acquire the lock or return a FinalizeSwapResponse with an error message.
        if let Err(error_message) = self.lock_finalize_swap() {
            return FinalizeSwapResponse::with_error(error_message);
        }

        // The lock is now acquired and asynchronous calls to finalize are blocked.
        // Perform all subactions.
        let finalize_swap_response = self.finalize_inner(now_fn, environment).await;

        if finalize_swap_response.has_error_message() {
            log!(
                ERROR,
                "The swap did not finalize successfully. \n\
                finalize_swap_response: {finalize_swap_response:?}"
            );
        } else {
            log!(
                INFO,
                "The swap finalized successfully. \n\
                finalize_swap_response: {finalize_swap_response:?}"
            );
        }

        // Release the lock. Note, if there is a panic, the lock will
        // not be released. In that case, the Swap canister will need
        // to be upgraded to release the lock.
        self.unlock_finalize_swap();

        finalize_swap_response
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

**File:** rs/sns/swap/src/types.rs (L895-902)
```rust
    pub fn set_sweep_icp_result(&mut self, sweep_icp_result: SweepResult) {
        if !sweep_icp_result.is_successful_sweep() {
            self.set_error_message(
                "Transferring ICP did not complete fully, some transfers were invalid or failed. Halting swap finalization".to_string()
            );
        }
        self.sweep_icp_result = Some(sweep_icp_result);
    }
```

**File:** rs/sns/swap/canister/canister.rs (L149-159)
```rust
/// See Swap.finalize.
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

**File:** rs/sns/swap/tests/swap.rs (L2585-2654)
```rust
async fn test_finalization_halts_when_sweep_icp_fails() {
    // Step 1: Prepare the world

    // Setup the necessary buyers for the test
    let mut swap = Swap {
        lifecycle: Committed as i32,
        init: Some(init()),
        params: Some(params()),
        buyers: btreemap! {
            // This Buyer is `Invalid` because the amount committed is less than the
            // DEFAULT_TRANSFER_FEE of the ICP Ledger. This should never be possible
            // in production, but sweep_icp must handle this case.
            i2principal_id_string(1000) => BuyerState {
                icp: Some(TransferableAmount {
                    amount_e8s: DEFAULT_TRANSFER_FEE.get_e8s() - 1,
                    ..Default::default()
                }),
                has_created_neuron_recipes: Some(false),
            },
            // This buyer's state is valid, but a mock call to the ledger will fail the transfer,
            // which should result in a failure field increment.
            i2principal_id_string(1003) => BuyerState {
                icp: Some(TransferableAmount {
                    amount_e8s: 10 * E8,
                    ..Default::default()
                }),
                has_created_neuron_recipes: Some(false),
            },
        },
        ..Default::default()
    };

    let mut clients = CanisterClients {
        icp_ledger: SpyLedger::new(vec![
            // This mocked reply should produce a successful transfer in SweepResult
            LedgerReply::TransferFunds(Err(NervousSystemError::new_with_message(
                "Error when transferring funds",
            ))),
        ]),
        ..spy_clients()
    };

    // Step 2: Call sweep_icp
    let result = swap.finalize(now_fn, &mut clients).await;

    assert_eq!(
        result.sweep_icp_result,
        Some(SweepResult {
            success: 0,
            skipped: 0,
            failure: 1, // Single failed transfer
            invalid: 1, // Single invalid buyer
            global_failures: 0,
        })
    );

    assert_eq!(
        result.error_message,
        Some(String::from(
            "Transferring ICP did not complete fully, some transfers were invalid or failed. Halting swap finalization"
        ))
    );

    // Assert that all other fields are set to None because finalization was halted.
    assert!(result.settle_neurons_fund_participation_result.is_none());
    assert!(result.set_dapp_controllers_call_result.is_none());
    assert!(result.create_sns_neuron_recipes_result.is_none());
    assert!(result.sweep_sns_result.is_none());
    assert!(result.set_mode_call_result.is_none());
    assert!(result.claim_neuron_result.is_none());
```
