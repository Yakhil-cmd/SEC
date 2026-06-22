### Title
SNS Swap `finalize_inner` Halts Entirely When Any Single Buyer Transfer Is Invalid, Permanently Blocking All Other Participants' Funds - (`rs/sns/swap/src/swap.rs`)

### Summary

The SNS Swap canister's `finalize_inner` function executes a sequential multi-step finalization pipeline. If any single buyer's ICP transfer produces an `invalid` result during `sweep_icp`, the entire pipeline halts permanently — blocking all other participants from receiving their SNS tokens, ICP refunds, and preventing SNS governance from transitioning to Normal mode. This is a direct analog to the reported vulnerability class: partial failure in a multi-step process causes all funds to be stuck.

### Finding Description

`finalize_inner` in `rs/sns/swap/src/swap.rs` executes the following sequential steps:

1. `sweep_icp` — transfers ICP from buyer subaccounts to SNS governance (or back to buyers if aborted)
2. `settle_neurons_fund_participation`
3. `create_sns_neuron_recipes`
4. `sweep_sns` — distributes SNS tokens to buyers
5. `claim_swap_neurons`
6. `set_sns_governance_to_normal_mode`

After each step, the pipeline checks `has_error_message()` and returns early if set: [1](#0-0) 

The error message is set by `set_sweep_icp_result` whenever `is_successful_sweep()` returns `false`: [2](#0-1) 

`is_successful_sweep()` returns `false` if **any** buyer has `failure > 0`, `invalid > 0`, or `global_failures > 0`: [3](#0-2) 

Inside `sweep_icp`, a buyer is marked `invalid` (not retryable) in three cases:
- The principal string cannot be parsed (corrupted state)
- `buyer_state.icp` is `None` (corrupted `BuyerState`)
- `TransferResult::AmountTooSmall` — the committed amount is less than `DEFAULT_TRANSFER_FEE` [4](#0-3) 

The code itself acknowledges the `invalid` case requires manual intervention:

> "This will require a manual intervention via an upgrade to correct" [5](#0-4) 

The `invalid` case is explicitly tested and confirmed to halt the entire finalization: [6](#0-5) 

### Impact Explanation

When `sweep_icp` returns any `invalid` count (even for a single buyer out of thousands), `finalize_inner` returns immediately with an error. All subsequent steps are skipped:

- **COMMITTED swap**: `sweep_sns` never runs → all buyers' SNS tokens remain locked in the swap canister. `claim_swap_neurons` never runs → no neurons are created. `set_sns_governance_to_normal_mode` never runs → SNS governance stays in `PreInitializationSwap` mode, blocking all governance operations.
- **ABORTED swap**: `sweep_icp` itself iterates all buyers and marks each individually, but if any buyer is `invalid`, the pipeline halts before `settle_neurons_fund_participation` and `restore_dapp_controllers` run.

Since `invalid` is explicitly defined as "will not be successful on this call and all future calls," the funds remain stuck until a canister upgrade is deployed to remove or fix the invalid buyer entry. This matches the reported vulnerability: one stuck participant blocks all others. [7](#0-6) 

### Likelihood Explanation

The `AmountTooSmall` path (amount ≤ `DEFAULT_TRANSFER_FEE`) is documented as "should never be possible in production" because `refresh_buyer_tokens` enforces a minimum. However:

- If the ICP ledger's transfer fee is ever increased via governance, previously valid buyer amounts could become too small at finalization time.
- State corruption (e.g., `buyer_state.icp = None`) could arise from upgrade bugs.
- The `invalid` path is reachable without any attacker — it is a latent design flaw triggered by edge-case conditions.

Likelihood is **low** for any single swap, but the consequence when triggered is severe and requires a privileged canister upgrade to resolve.

### Recommendation

1. In `finalize_inner`, treat `invalid` sweep results as non-blocking: allow the pipeline to continue past `sweep_icp` even when some buyers are `invalid`, since those buyers' funds are already unrecoverable without an upgrade.
2. Alternatively, separate the `invalid` check from the `failure` check in `is_successful_sweep` — only halt on `failure` (retryable) and `global_failures`, not on `invalid` (already unrecoverable).
3. Add a governance-controlled mechanism to remove or zero-out invalid buyer entries without a full canister upgrade. [3](#0-2) 

### Proof of Concept

1. A committed SNS swap has N buyers, one of whom has `amount_e8s == DEFAULT_TRANSFER_FEE - 1` (e.g., due to a fee increase after participation).
2. `finalize` is called. `sweep_icp` iterates all buyers: N-1 succeed, 1 is marked `invalid`.
3. `set_sweep_icp_result` sees `invalid == 1`, sets `error_message = "Transferring ICP did not complete fully..."`.
4. `finalize_inner` checks `has_error_message()` → `true` → returns immediately.
5. `sweep_sns`, `claim_swap_neurons`, `set_mode` are never called.
6. All N-1 valid buyers' SNS tokens remain locked in the swap canister indefinitely.
7. Repeated calls to `finalize` produce the same result (the invalid buyer is always invalid).
8. Resolution requires a canister upgrade to remove the invalid buyer entry. [8](#0-7)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1544-1598)
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
```

**File:** rs/sns/swap/src/swap.rs (L2070-2139)
```rust
        for (principal_str, buyer_state) in self.buyers.iter_mut() {
            // principal_str should always be parseable as a PrincipalId as that is enforced
            // in `refresh_buyer_tokens`. In the case of a bug due to programmer error, increment
            // the invalid field. This will require a manual intervention via an upgrade to correct
            let principal = match string_to_principal(principal_str) {
                Some(p) => p,
                None => {
                    sweep_result.invalid += 1;
                    continue;
                }
            };

            let subaccount = principal_to_subaccount(&principal);
            let dst = if lifecycle == Lifecycle::Committed {
                // This Account should be given a name, such as SNS ICP Treasury...
                Account {
                    owner: sns_governance.get().0,
                    subaccount: None,
                }
            } else {
                Account {
                    owner: principal.0,
                    subaccount: None,
                }
            };

            let icp_transferable_amount = match buyer_state.icp.as_mut() {
                Some(transferable_amount) => transferable_amount,
                // BuyerState.icp should always be present as it is set in `refresh_buyer_tokens`.
                // In the case of a bug due to programmer error, increment the invalid field.
                // This will require a manual intervention via an upgrade to correct
                None => {
                    log!(
                        ERROR,
                        "PrincipalId {} has corrupted BuyerState: {:?}",
                        principal,
                        buyer_state
                    );
                    sweep_result.invalid += 1;
                    continue;
                }
            };

            let result = icp_transferable_amount
                .transfer_helper(
                    now_fn,
                    DEFAULT_TRANSFER_FEE,
                    Some(subaccount),
                    &dst,
                    icp_ledger,
                )
                .await;
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

**File:** rs/sns/swap/src/types.rs (L969-978)
```rust
    fn is_successful_sweep(&self) -> bool {
        let SweepResult {
            failure,
            invalid,
            success: _,
            skipped: _,
            global_failures,
        } = self;
        *failure == 0 && *invalid == 0 && *global_failures == 0
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L896-899)
```text
  // Invalid means that on this call and all future calls to finalize,
  // this item will not be successful, and will need intervention to
  // succeed.
  uint32 invalid = 4;
```
