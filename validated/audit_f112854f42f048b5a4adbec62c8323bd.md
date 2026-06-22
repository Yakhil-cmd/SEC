### Title
SNS Swap `finalize_inner` Permanently Halted by Invalid Buyer Entry in `sweep_icp` — (File: `rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS Swap canister's `finalize_inner` function treats a permanent, non-retryable `invalid` buyer entry in `sweep_icp` identically to a transient `failure`, halting all subsequent finalization steps forever. If any buyer's `amount_e8s` is at or below `DEFAULT_TRANSFER_FEE`, every call to `finalize` will permanently stall, locking all SNS tokens in the swap canister and leaving SNS governance permanently in restricted mode.

---

### Finding Description

`finalize_inner` in `rs/sns/swap/src/swap.rs` executes a strict sequential pipeline:

1. `sweep_icp` — transfer ICP from buyer subaccounts
2. `settle_neurons_fund_participation`
3. `create_sns_neuron_recipes`
4. `sweep_sns` — distribute SNS tokens
5. `claim_swap_neurons`
6. `set_sns_governance_to_normal_mode` [1](#0-0) 

After `sweep_icp` completes, `set_sweep_icp_result` is called: [2](#0-1) 

`is_successful_sweep()` returns `false` whenever `failure > 0` **or** `invalid > 0`. When it returns `false`, `set_sweep_icp_result` sets an error message and `finalize_inner` returns early, skipping all remaining steps.

The `invalid` counter is incremented inside `transfer_helper` when `amount_e8s <= fee`: [3](#0-2) 

Critically, `transfer_helper` returns `AmountTooSmall` **without modifying any state** — `transfer_start_timestamp_seconds` remains `0`. This means the condition is **permanent and non-retryable**: every subsequent call to `finalize` will encounter the same buyer, increment `invalid`, and halt finalization again.

The code comment acknowledges this: *"This should never be possible in production, but sweep_icp must handle this case."* However, no mechanism prevents finalization from being permanently blocked if such a state exists. [4](#0-3) 

The test `test_finalization_halts_when_sweep_icp_fails` confirms that even a single `invalid: 1` entry causes `finalize` to halt with error message `"Transferring ICP did not complete fully, some transfers were invalid or failed. Halting swap finalization"` and all downstream fields set to `None`. [5](#0-4) 

---

### Impact Explanation

If finalization is permanently blocked:

- **SNS governance remains in restricted mode** — `set_sns_governance_to_normal_mode` is never reached (step 6), so the SNS governance canister cannot accept proposals or operate normally.
- **All SNS tokens are locked** — `sweep_sns` (step 4) is never reached; SNS tokens allocated to participants remain in the swap canister forever.
- **Neurons' Fund maturity is unrecoverable** — `settle_neurons_fund_participation` (step 2) is never called; NF maturity already deducted at proposal adoption is neither refunded nor converted to SNS neurons.
- **Dapp control is not transferred** — `take_sole_control_of_dapp_controllers` (step 6 branch) is never reached; the dapp remains under swap canister control.

The swap canister is stuck in `COMMITTED` state with no path to recovery except a canister upgrade. [6](#0-5) 

---

### Likelihood Explanation

The `AmountTooSmall` condition arises when a buyer's `amount_e8s <= DEFAULT_TRANSFER_FEE` (10,000 e8s = 0.0001 ICP). This requires `min_participant_icp_e8s` to be set at or below 10,000 e8s in the swap parameters. While most production SNS swaps use much larger minimums, the swap parameter validation does not enforce a floor above `DEFAULT_TRANSFER_FEE`. An SNS launched with a very small minimum participation threshold (e.g., for testing or niche use cases) is directly vulnerable. Additionally, any future state corruption (e.g., via canister upgrade bugs) that introduces an `invalid` buyer entry would trigger the same permanent halt. [7](#0-6) 

---

### Recommendation

1. **Separate `invalid` from `failure` in `is_successful_sweep`**: `invalid` entries represent permanent, non-retryable conditions; they should not block finalization of all other buyers. Consider treating `invalid` as a warning (logged, counted) rather than a fatal error that halts the pipeline.

2. **Skip `invalid` entries silently**: `finalize_inner` should proceed to subsequent steps even when `invalid > 0`, since those entries will never succeed regardless of retries.

3. **Enforce `min_participant_icp_e8s > DEFAULT_TRANSFER_FEE`** in swap parameter validation to prevent the `AmountTooSmall` condition from arising in production.

4. **Add a recovery path**: Provide an admin/upgrade hook that can remove or zero out permanently-invalid buyer entries without requiring a full canister upgrade.

---

### Proof of Concept

1. Deploy an SNS swap with `min_participant_icp_e8s = 5_000` (below `DEFAULT_TRANSFER_FEE = 10_000`).
2. Buyer A calls `refresh_buyer_tokens` after transferring 5,000 e8s ICP to their subaccount. The buyer state is recorded with `amount_e8s = 5_000`.
3. The swap reaches `COMMITTED` state (other buyers meet the minimum participation threshold).
4. Any caller invokes `finalize_swap`.
5. `sweep_icp` iterates over buyers; for Buyer A, `transfer_helper` returns `AmountTooSmall` → `invalid += 1`.
6. `set_sweep_icp_result` sees `invalid = 1`, sets error message, `finalize_inner` returns early.
7. Steps 2–6 of `finalize_inner` are never executed.
8. Every subsequent call to `finalize_swap` repeats steps 5–7 identically, since no state is modified for Buyer A.
9. SNS governance remains in restricted mode; all SNS tokens remain locked in the swap canister permanently. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1556-1561)
```rust
        // Transfer the ICP tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }
```

**File:** rs/sns/swap/src/swap.rs (L1593-1624)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L2122-2139)
```rust
            match result {
                // AmountToSmall should never happen as the amount contributed is checked in
                // `refresh_buyer_tokens`. In the case of a bug due to programmer error,
                // increment the invalid field. This will require a manual intervention
                // via an upgrade to correct
                TransferResult::AmountTooSmall => {
                    sweep_result.invalid += 1;
                }
                TransferResult::AlreadyStarted => {
                    sweep_result.skipped += 1;
                }
                TransferResult::Success(_) => {
                    sweep_result.success += 1;
                }
                TransferResult::Failure(_) => {
                    sweep_result.failure += 1;
                }
            }
```

**File:** rs/sns/swap/src/types.rs (L612-616)
```rust
        let amount = Tokens::from_e8s(self.amount_e8s);
        if amount <= fee {
            // Skip: amount too small...
            return TransferResult::AmountTooSmall;
        }
```

**File:** rs/sns/swap/src/types.rs (L617-621)
```rust
        if self.transfer_start_timestamp_seconds > 0 {
            // Operation in progress...
            return TransferResult::AlreadyStarted;
        }
        self.transfer_start_timestamp_seconds = now_fn(false);
```

**File:** rs/sns/swap/src/types.rs (L654-665)
```rust
            Err(e) => {
                self.transfer_start_timestamp_seconds = 0;
                self.transfer_success_timestamp_seconds = 0;
                log!(
                    ERROR,
                    "Failed to transfer {} from subaccount {:#?}: {}",
                    amount,
                    subaccount,
                    e
                );
                TransferResult::Failure(e.to_string())
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

**File:** rs/sns/swap/tests/swap.rs (L2583-2655)
```rust
/// Tests that if transferring does not complete fully, finalize will halt finalization
#[tokio::test]
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
}
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L601-604)
```text
  // greater than or equal to `min_participant_icp_e8s` and less than
  // or equal to `max_icp_e8s`. Can effectively be disabled by
  // setting it to `max_icp_e8s`.
  uint64 max_participant_icp_e8s = 5;
```
