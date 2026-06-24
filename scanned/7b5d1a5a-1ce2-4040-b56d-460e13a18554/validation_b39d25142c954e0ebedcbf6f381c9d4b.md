### Title
Single Failing ICP Transfer in `sweep_icp` Blocks Entire SNS Swap Finalization Pipeline - (`File: rs/sns/swap/src/swap.rs`)

### Summary
The `finalize_inner` function in the SNS Swap canister halts the entire swap finalization pipeline when any single buyer's ICP transfer fails during `sweep_icp`. A single participant with a transient ledger error causes all subsequent finalization steps — SNS token distribution, neuron creation, and governance mode transition — to be permanently blocked until the failing transfer is retried and succeeds. This is the IC analog of the reported Solidity bug: a single participant with insufficient/failing settlement blocks the processing of all other participants.

### Finding Description
In `rs/sns/swap/src/swap.rs`, `finalize_inner` calls `sweep_icp` and then immediately checks `has_error_message()`:

```rust
finalize_swap_response
    .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
if finalize_swap_response.has_error_message() {
    return finalize_swap_response;  // entire pipeline halted
}
```

`set_sweep_icp_result` in `rs/sns/swap/src/types.rs` sets an error message whenever the sweep is not fully successful — including when even a single buyer's transfer returns `TransferResult::Failure`:

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

`is_successful_sweep()` returns `false` if `failure > 0` OR `invalid > 0`. A single buyer whose ICP transfer fails (e.g., due to a transient ledger error, or whose `BuyerState` is corrupted/invalid) causes the entire `finalize_inner` to return early, blocking:
- `settle_neurons_fund_participation`
- `create_sns_neuron_recipes`
- `sweep_sns` (SNS token distribution to all participants)
- `claim_swap_neurons`
- `set_sns_governance_to_normal_mode`

The `sweep_icp` loop itself does continue past individual failures (it increments `sweep_result.failure` and `continue`s), so the per-buyer iteration is not blocked. However, the **pipeline-level** check after `sweep_icp` returns treats any partial failure as a hard stop for all subsequent steps.

The `invalid` case is particularly severe: a buyer with `amount_e8s < DEFAULT_TRANSFER_FEE` is classified as `invalid` (not retryable), yet it still sets the error message and halts finalization for all other participants.

### Impact Explanation
When a committed SNS swap has even one buyer whose ICP transfer fails or is classified invalid:
1. **SNS tokens are never distributed** to any participant (including those whose ICP transfers succeeded).
2. **SNS neurons are never created** for any participant.
3. **SNS Governance remains in `PreInitializationSwap` mode** indefinitely — the SNS is stuck and cannot operate.
4. **Neurons' Fund maturity** may remain locked if `settle_neurons_fund_participation` is also blocked.

The SNS is effectively bricked until the failing transfer is resolved. Since `finalize` can be called again (it is idempotent for already-succeeded steps via the `AlreadyStarted`/`skipped` mechanism), the pipeline will eventually complete once the failing buyer's transfer succeeds on retry. However, the `invalid` case (e.g., `amount_e8s < fee`) is **permanently unretryable** — it will always produce `invalid += 1`, always trigger `!is_successful_sweep()`, and always halt finalization. This means a single buyer with an invalid amount permanently prevents all other participants from receiving their SNS tokens and neurons.

### Likelihood Explanation
The `invalid` path is reachable in practice: `BuyerState` with `amount_e8s < DEFAULT_TRANSFER_FEE` is explicitly acknowledged in the code as a case that "should never happen in production" but is handled defensively. If such a state is ever reached (e.g., due to a fee parameter change after participation, or a bug in `refresh_buyer_tokens`), the swap is permanently stuck. The transient `failure` path is more common and self-resolving on retry, but still blocks all other participants during each failed finalization attempt. Any unprivileged swap participant who participates with a minimal amount near the fee boundary could trigger this condition if fees change.

### Recommendation
1. **Decouple pipeline halting from `invalid` vs `failure`**: Only halt the pipeline on `global_failures` (corrupted state). Individual `failure` and `invalid` entries should not block subsequent pipeline stages — the pipeline should proceed and mark those buyers as permanently failed.
2. **Separate `invalid` from `failure` in the halt condition**: `invalid` entries are unretryable and should not prevent the pipeline from completing for all other participants. Consider treating `invalid` buyers as permanently skipped and proceeding.
3. **Alternatively**: Move the `is_successful_sweep()` check to only block on `global_failures > 0`, allowing the pipeline to proceed even when some individual transfers fail or are invalid.

### Proof of Concept
The vulnerability is directly confirmed by the existing test `test_finalization_halts_when_sweep_icp_fails` in `rs/sns/swap/tests/swap.rs`:

```rust
// A swap with one invalid buyer (amount < fee) and one failing transfer
// causes ALL subsequent finalization steps to be None (blocked):
assert!(result.settle_neurons_fund_participation_result.is_none());
assert!(result.set_dapp_controllers_call_result.is_none());
assert!(result.create_sns_neuron_recipes_result.is_none());
assert!(result.sweep_sns_result.is_none());
assert!(result.set_mode_call_result.is_none());
assert!(result.claim_neuron_result.is_none());
```

The root cause chain:
1. `finalize_inner` calls `sweep_icp` → one buyer's transfer fails → `SweepResult { failure: 1, invalid: 1, ... }` [1](#0-0) 
2. `set_sweep_icp_result` checks `is_successful_sweep()` → `false` because `failure > 0` or `invalid > 0` → sets error message [2](#0-1) 
3. `finalize_inner` checks `has_error_message()` → `true` → returns early, blocking all remaining steps [3](#0-2) 
4. The `invalid` case in `sweep_icp` is explicitly unretryable — it will always block finalization on every future call [4](#0-3) 
5. Test confirms the full pipeline halt behavior [5](#0-4)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1557-1561)
```rust
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }
```

**File:** rs/sns/swap/src/swap.rs (L2122-2138)
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
