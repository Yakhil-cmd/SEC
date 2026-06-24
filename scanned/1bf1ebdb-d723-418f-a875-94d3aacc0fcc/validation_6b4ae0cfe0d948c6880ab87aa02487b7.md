### Title
SNS Swap Canister Does Not Return SNS Tokens to Treasury on Abort — (`rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS Swap canister is a single-price auction where SNS tokens are pre-loaded and ICP is contributed by buyers. When the swap is aborted (insufficient participation), `finalize_inner` refunds ICP to buyers and restores dapp controllers, but **returns early without ever returning the SNS tokens to the SNS governance treasury**. Those tokens remain permanently locked in the swap canister's ledger account with no recovery path.

---

### Finding Description

The `finalize_inner` function in `rs/sns/swap/src/swap.rs` handles both the `COMMITTED` and `ABORTED` terminal states. In the `ABORTED` path, the execution flow is:

1. `sweep_icp` — refunds ICP to buyers ✓
2. `settle_neurons_fund_participation` — notifies NNS governance ✓
3. `should_restore_dapp_control()` returns `true` → restores dapp controllers → **returns early** [1](#0-0) 

The `sweep_sns` call — which distributes SNS tokens — is only reached **after** this early return, meaning it is never executed in the `ABORTED` lifecycle: [2](#0-1) 

Furthermore, `sweep_sns` itself explicitly guards against being called outside `COMMITTED`: [3](#0-2) 

There is no other code path in the swap canister that transfers SNS tokens back to the SNS governance canister or treasury when the lifecycle is `ABORTED`. The SNS tokens loaded into the swap canister's account on the SNS ledger remain there indefinitely, with no callable function to recover them.

The lifecycle diagram in the proto documentation confirms the swap canister is eventually deleted after abort: [4](#0-3) 

Once the swap canister is deleted, its principal can no longer authorize transfers from its SNS ledger account, making the tokens permanently unrecoverable.

The `FinalizeSwapResponse` proto confirms `sweep_sns_result` is a field of the response, but it is never populated in the abort path: [5](#0-4) 

---

### Impact Explanation

All SNS tokens pre-loaded into the swap canister before the swap opens are permanently lost when the swap is aborted. The SNS ledger account `(swap_canister_id, None)` holds the tokens, but after the swap canister is deleted, no principal can authorize a transfer from that account. This is a **ledger conservation bug**: tokens are minted/transferred into the system but have no exit path on the abort branch, violating the invariant that all tokens are either distributed to participants or returned to the treasury.

---

### Likelihood Explanation

Any SNS decentralization swap that fails to reach `min_participants` or `min_icp_e8s` before the deadline will transition to `ABORTED`. This is a normal, expected failure mode explicitly documented in the lifecycle. No adversarial action is required — a swap with low community interest will naturally abort. The `try_abort` path is exercised in existing tests: [6](#0-5) 

---

### Recommendation

In `finalize_inner`, before the early return in the `should_restore_dapp_control()` branch, add a call to transfer the remaining SNS token balance from the swap canister's account back to the SNS governance treasury (or a designated fallback address). Alternatively, add a dedicated `sweep_sns_to_treasury` function callable only in the `ABORTED` state that transfers the full SNS token balance back to `init.sns_governance_canister_id`.

The fix is analogous to the recommendation in the external report: pass an explicit `owner`/`treasury` address so that on abort, tokens are returned to a principal that can actually use them.

---

### Proof of Concept

The abort path in `finalize_inner` can be traced directly:

1. Swap is opened with SNS tokens pre-loaded (e.g., `with_sns_tokens(10 * E8)` as in existing tests).
2. No buyers participate, or participation falls below `min_icp_e8s`.
3. `try_abort` transitions lifecycle to `ABORTED`.
4. `finalize` is called → `finalize_inner` runs `sweep_icp` (no buyers to refund, or refunds them), then hits `should_restore_dapp_control() == true` and returns early.
5. `sweep_sns` is never called.
6. The SNS ledger account of the swap canister retains the full `sns_tokens` balance.
7. The swap canister is deleted per the lifecycle diagram.
8. The SNS tokens are permanently inaccessible.

The existing test `test_finalize_swap_abort_matched_funding` confirms the abort path completes without any `sweep_sns_result` being set: [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1572-1584)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L4636-4669)
```rust
    #[test]
    fn test_try_commit_or_abort_insufficient_participation_with_no_time_remaining() {
        let sale_duration = 100;
        let time_remaining = 0;
        let now = sale_duration - time_remaining;
        let buyers = BTreeMap::new();
        let mut swap = SwapBuilder::new()
            .with_lifecycle(Lifecycle::Open)
            .with_buyers(buyers)
            .with_swap_start_due(None, Some(sale_duration))
            .with_min_participants(1)
            .with_min_max_participant_icp(10, 20)
            .with_min_max_direct_participation(10, 20)
            .build();

        // test try_commit
        {
            let mut swap = swap.clone();
            let result = swap.try_commit(now);
            // swap should not commit because there is no time remaining and we
            // have not reached the minimum number of participants

            assert!(!result);
            assert_eq!(swap.lifecycle, Lifecycle::Open as i32);
        }
        // test try_abort
        {
            let result = swap.try_abort(now);
            // swap should abort because there is no time remaining and we
            // have not reached the minimum number of participants

            assert!(result);
            assert_eq!(swap.lifecycle, Lifecycle::Aborted as i32);
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L862-866)
```text
message FinalizeSwapResponse {
  SweepResult sweep_icp_result = 1;

  SweepResult sweep_sns_result = 2;

```

**File:** rs/sns/swap/tests/swap.rs (L1557-1600)
```rust
#[tokio::test]
async fn test_finalize_swap_abort_matched_funding() {
    // Step 1: Prepare the world.

    let buyers = btreemap! {
        i2principal_id_string(8502) => BuyerState::new(77 * E8),
    };
    let mut swap = SwapBuilder::new()
        .with_sns_governance_canister_id(SNS_GOVERNANCE_CANISTER_ID)
        .with_nns_proposal_id(OPEN_SNS_TOKEN_SWAP_PROPOSAL_ID)
        .with_lifecycle(Open)
        .with_swap_start_due(Some(START_TIMESTAMP_SECONDS), Some(END_TIMESTAMP_SECONDS))
        .with_min_participants(1)
        .with_min_max_participant_icp(1, 100)
        .with_min_max_direct_participation(36_000, 45_000)
        .with_sns_tokens(10 * E8)
        .with_neuron_basket_count(3)
        .with_neuron_basket_dissolve_delay_interval(7890000) // 3 months
        .with_neurons_fund_participation()
        .with_buyers(buyers.clone())
        .build();

    let buyer_principal_id = PrincipalId::new_user_test_id(8502);

    // Step 1.5: Attempt to auto-finalize the swap. It should not work, since
    // the swap is open. Not only should it not work, it should do nothing.
    assert_eq!(swap.lifecycle(), Open);
    assert_eq!(swap.already_tried_to_auto_finalize, Some(false));
    assert_eq!(swap.auto_finalize_swap_response, None);
    let auto_finalization_error = swap
        .try_auto_finalize(now_fn, &mut spy_clients_exploding_root())
        .await
        .unwrap_err();
    let allowed_to_finalize_error = swap.can_finalize().unwrap_err();
    assert_eq!(auto_finalization_error, allowed_to_finalize_error);

    // already_tried_to_auto_finalize should still be set to false, since it
    // couldn't try to auto-finalize due to the swap not being committed.
    assert_eq!(swap.already_tried_to_auto_finalize, Some(false));
    assert_eq!(swap.auto_finalize_swap_response, None);

    // Step 2: Abort the swap
    assert!(swap.try_abort(/* now_seconds: */ END_TIMESTAMP_SECONDS + 1));
    assert_eq!(swap.lifecycle(), Aborted);
```
