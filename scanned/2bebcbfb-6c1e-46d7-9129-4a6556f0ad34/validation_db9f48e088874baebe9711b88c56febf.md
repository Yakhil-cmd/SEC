### Title
SNS Tokens Permanently Locked in Swap Canister When Swap Is Aborted — (File: `rs/sns/swap/src/swap.rs`)

---

### Summary
When an SNS decentralization swap transitions to `LIFECYCLE_ABORTED` (analogous to the vesting period beginning in the original report), the SNS tokens pre-deposited into the swap canister are never returned to the SNS governance treasury. The `finalize_inner` function takes an early-return path for aborted swaps that skips `sweep_sns` entirely, leaving all unsold SNS tokens permanently locked in the swap canister's ledger account with no on-chain recovery mechanism.

---

### Finding Description
The SNS swap canister is pre-funded with SNS tokens before the swap opens. When the swap is **committed**, `finalize_inner` calls `create_sns_neuron_recipes` and then `sweep_sns`, which transfers SNS tokens from the swap canister's account on the SNS ledger to each buyer's neuron subaccount.

However, when the swap is **aborted** (insufficient participation before `swap_due_timestamp_seconds`), `finalize_inner` follows a different code path: [1](#0-0) 

The function calls `sweep_icp` (refunds ICP to buyers), settles the Neurons' Fund, then enters the `should_restore_dapp_control()` branch, restores dapp controllers to fallback principals, and **returns immediately**. The comment explicitly states "finalize() need not do any more work." The `sweep_sns` call — which is the only mechanism to move SNS tokens out of the swap canister — is never reached. [2](#0-1) 

Furthermore, `sweep_sns` itself enforces that it can only run in `LIFECYCLE_COMMITTED` state: [3](#0-2) 

So even a direct call to `sweep_sns` after abort would fail with a global error. There is no other function in the swap canister that transfers SNS tokens back to governance or treasury.

The lifecycle state machine confirms that `ABORTED` is a terminal state with no path back to `OPEN` or `COMMITTED`: [4](#0-3) 

The `FinalizeSwapResponse` proto confirms `sweep_sns_result` is absent in the aborted finalization path, as observed in integration tests: [5](#0-4) 

---

### Impact Explanation
All SNS tokens deposited into the swap canister before the swap opened remain permanently locked in the swap canister's account on the SNS ledger after an abort. These tokens are inaccessible to the SNS governance treasury, the SNS root, and any other canister. The SNS token supply is conserved on the ledger but the tokens are unrecoverable without an NNS-level canister upgrade to the swap canister — a manual, governance-gated intervention that is not guaranteed to occur. This is a **ledger conservation bug**: tokens are neither distributed to buyers nor returned to the treasury.

---

### Likelihood Explanation
An aborted swap is a realistic and common outcome. Any SNS swap that fails to reach `min_direct_participation_icp_e8s` by `swap_due_timestamp_seconds` will abort. No attacker action is required — natural market conditions (low participation, unfavorable token economics, regulatory concerns) are sufficient. The swap parameters are set at initialization and cannot be changed, so there is no way to extend the swap or lower the minimum after the fact. [6](#0-5) 

---

### Recommendation
In `finalize_inner`, before returning early on the aborted path, add a step that transfers all remaining SNS tokens from the swap canister's account back to the SNS governance treasury account (or a designated recovery account). This is analogous to the `withdrawUnallocatedIntx` fix applied in the original report. Specifically:

1. After `restore_dapp_controllers_for_finalize` succeeds in the aborted branch, call a new `sweep_sns_to_treasury` function that transfers the swap canister's entire SNS ledger balance to the SNS governance canister's main account.
2. Alternatively, add a permissioned `recover_sns_tokens` endpoint callable by the NNS governance canister that performs this transfer at any time after the swap reaches a terminal state.

---

### Proof of Concept
1. An SNS is created with `initial_swap_amount_e8s = 10_000_000 * E8` SNS tokens deposited into the swap canister.
2. The swap opens. Only `min_direct_participation_icp_e8s - 1` ICP is contributed by direct participants.
3. `swap_due_timestamp_seconds` elapses. The heartbeat calls `try_abort`, transitioning the swap to `LIFECYCLE_ABORTED`.
4. Anyone calls `finalize_swap`. `finalize_inner` executes: `sweep_icp` refunds the ICP contributors; `settle_neurons_fund_participation` is called; `should_restore_dapp_control()` returns `true`; dapp controllers are restored; the function **returns early**.
5. `sweep_sns` is never called. The `10_000_000 * E8` SNS tokens remain in the swap canister's SNS ledger account indefinitely.
6. Calling `sweep_sns` directly fails because `self.lifecycle() != Lifecycle::Committed`.
7. The SNS governance canister remains in `PreInitializationSwap` mode and cannot execute proposals to recover the tokens. Only an NNS proposal to upgrade the swap canister could add a recovery path. [7](#0-6)

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

**File:** rs/sns/swap/src/swap.rs (L2170-2178)
```rust
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L361-388)
```text

  // The number of ICP that is "targeted" by this token swap. If this
  // amount is achieved with sufficient participation, the swap will be
  // triggered immediately, without waiting for the due date
  // (`end_timestamp_seconds`). This means that an investor knows the minimum
  // number of SNS tokens received per invested ICP. If this amount is achieved
  // without reaching sufficient_participation, the swap will abort without
  // waiting for the due date. Must be at least
  // `min_participants * min_participant_icp_e8s`.
  optional uint64 max_direct_participation_icp_e8s = 31;

  // The minimum amount of ICP that each buyer must contribute to
  // participate. Must be greater than zero.
  optional uint64 min_participant_icp_e8s = 20;

  // The maximum amount of ICP that each buyer can contribute. Must be
  // greater than or equal to `min_participant_icp_e8s` and less than
  // or equal to `max_icp_e8s`. Can effectively be disabled by
  // setting it to `max_icp_e8s`.
  optional uint64 max_participant_icp_e8s = 21;

  // The date/time when the swap should start.
  optional uint64 swap_start_timestamp_seconds = 22;

  // The date/time when the swap is due, i.e., it will automatically
  // end and commit or abort depending on whether the parameters have
  // been fulfilled.
  optional uint64 swap_due_timestamp_seconds = 23;
```

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L950-961)
```rust
        let expected_sweep_sns_result =
            if swap_finalization_status == SwapFinalizationStatus::Aborted {
                None
            } else {
                Some(SweepResult {
                    success: 0,
                    failure: 0,
                    skipped: expected_neuron_count,
                    invalid: 0,
                    global_failures: 0,
                })
            };
```
