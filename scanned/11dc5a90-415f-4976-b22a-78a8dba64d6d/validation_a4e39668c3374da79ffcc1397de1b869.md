### Title
SNS Swap `finalize_inner` Permanently Halted by Single Participant Transfer Failure, Locking All Participants' Tokens - (File: `rs/sns/swap/src/swap.rs`)

### Summary
The SNS Swap canister's `finalize_inner` function halts the entire finalization pipeline if any single participant's ICP or SNS token transfer fails. This is a structural push-payment vulnerability: a single participant whose transfer permanently fails blocks all other participants from receiving their tokens and prevents SNS governance from ever entering normal mode.

### Finding Description
The `finalize_inner` function in `rs/sns/swap/src/swap.rs` orchestrates token distribution by calling `sweep_icp` and `sweep_sns` in sequence. Each sweep iterates over all participants and calls `transfer_helper` to push tokens to each participant's account. If any transfer fails, `TransferResult::Failure` is returned and `sweep_result.failure` is incremented. [1](#0-0) 

The `SweepResult` is then passed to `set_sweep_icp_result` (or `set_sweep_sns_result`), which calls `is_successful_sweep()`. If `failure > 0` or `invalid > 0`, an error message is set on the response: [2](#0-1) 

`finalize_inner` checks `has_error_message()` after each sweep step and returns early if set: [3](#0-2) [4](#0-3) 

This means a single participant's transfer failure blocks:
1. `sweep_sns` — SNS tokens never distributed to participants
2. `claim_neuron` — neurons never claimed on behalf of participants
3. `set_mode` — SNS governance never enters normal mode
4. In the ABORTED case: `restore_dapp_controllers` — dapp controllers never restored to original owners

The `transfer_helper` resets `transfer_start_timestamp_seconds = 0` on failure, allowing retry: [5](#0-4) 

However, if the transfer can never succeed — for example, because the SNS token ledger (an ICRC-1 canister) returns a permanent `GenericError` for a specific recipient account (e.g., via governance-controlled access control on the SNS token ledger) — `finalize_inner` is permanently blocked. The ICRC-1 `TransferError` type explicitly includes `GenericError { error_code: Nat, message: String }` as a valid permanent rejection: [6](#0-5) 

The root cause is structurally identical to the external report: a push-payment pattern where a transfer to a specific recipient can fail and block the entire operation, with no fallback claims mechanism.

### Impact Explanation
- **COMMITTED swap**: All participants' SNS tokens remain locked in the Swap canister indefinitely. Neurons are never claimed. SNS governance is permanently stuck in `PreInitializationSwap` mode, preventing any governance proposals from being executed.
- **ABORTED swap**: Dapp controllers are never restored to their original owners (the fallback controllers). The Neurons' Fund participation is never settled with NNS governance.
- In both cases, the ICP held in buyer subaccounts of the Swap canister is also effectively inaccessible: `error_refund_icp` blocks refunds while `transfer_success_timestamp_seconds == 0`, and `sweep_icp` is blocked by the same failure. [7](#0-6) 

### Likelihood Explanation
The ICP ledger does not implement a blacklist, so permanent ICP transfer failures are unlikely. However:
1. The SNS token ledger is a custom ICRC-1 canister. Any SNS can deploy a token ledger with governance-controlled access control. A governance proposal that blocks a specific account from receiving tokens would cause `sweep_sns` to permanently fail for that participant.
2. An attacker who participates in an SNS swap and then obtains a governance majority in the SNS (possible early in the SNS lifecycle) could vote to block their own account on the token ledger, permanently halting finalization.
3. Even without malicious intent, a transient ledger error that persists (e.g., the SNS token ledger canister being stopped) blocks finalization for all participants until resolved.

The entry path is fully unprivileged: any participant in an SNS swap can trigger this by controlling the ledger behavior for their own account.

### Recommendation
1. **Decouple sweep failures from pipeline halting**: Mark permanently unresolvable transfers as `invalid` (not `failure`) so they do not block subsequent finalization steps. Only `global_failures` should halt the pipeline.
2. **Adopt a claims pattern**: Instead of pushing tokens to participants, record failed transfers in a `claims[account]` map and provide a `claim()` endpoint for participants to pull their tokens. This is the exact mitigation recommended in the external report.
3. **Separate `sweep_icp` from `sweep_sns`**: Allow `sweep_sns`, `claim_neuron`, and `set_mode` to proceed even if some ICP refunds are pending, since ICP refunds and SNS token distribution are independent operations.

### Proof of Concept
1. Participate in an SNS swap with principal `P`.
2. After the swap commits, use SNS governance to add `P`'s account to a blocklist on the SNS token ledger (via a `GenericError` in `icrc1_transfer`).
3. Call `finalize` on the Swap canister.
4. `sweep_sns` calls `transfer_helper` for `P`'s account; the SNS ledger returns `GenericError`, causing `TransferResult::Failure`.
5. `sweep_result.failure = 1`; `set_sweep_sns_result` sets the error message.
6. `finalize_inner` returns early; `claim_neuron` and `set_mode` are never called.
7. All other participants' SNS tokens remain locked in the Swap canister. SNS governance is permanently in `PreInitializationSwap` mode. [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1557-1561)
```rust
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
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

**File:** rs/sns/swap/src/swap.rs (L1950-1960)
```rust
        if let Some(buyer_state) = self.buyers.get(&source_principal_id.to_string()) {
            if let Some(transfer) = &buyer_state.icp
                && transfer.transfer_success_timestamp_seconds == 0
            {
                // This buyer has ICP not yet disbursed using the normal mechanism.
                return ErrorRefundIcpResponse::new_precondition_error(format!(
                    "ICP cannot be refunded as principal {} has {} ICP (e8s) in escrow",
                    source_principal_id,
                    buyer_state.amount_icp_e8s()
                ));
            }
```

**File:** rs/sns/swap/src/swap.rs (L2083-2094)
```rust
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
```

**File:** rs/sns/swap/src/swap.rs (L2113-2138)
```rust
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

**File:** rs/sns/swap/src/types.rs (L895-901)
```rust
    pub fn set_sweep_icp_result(&mut self, sweep_icp_result: SweepResult) {
        if !sweep_icp_result.is_successful_sweep() {
            self.set_error_message(
                "Transferring ICP did not complete fully, some transfers were invalid or failed. Halting swap finalization".to_string()
            );
        }
        self.sweep_icp_result = Some(sweep_icp_result);
```

**File:** rs/sns/swap/src/types.rs (L912-919)
```rust
    pub fn set_sweep_sns_result(&mut self, sweep_sns_result: SweepResult) {
        if !sweep_sns_result.is_successful_sweep() {
            self.set_error_message(
                "Transferring SNS tokens did not complete fully, some transfers were invalid or failed. Halting swap finalization".to_string()
            );
        }
        self.sweep_sns_result = Some(sweep_sns_result);
    }
```

**File:** packages/icrc-ledger-types/src/icrc1/transfer.rs (L62-71)
```rust
pub enum TransferError {
    BadFee { expected_fee: NumTokens },
    BadBurn { min_burn_amount: NumTokens },
    InsufficientFunds { balance: NumTokens },
    TooOld,
    CreatedInFuture { ledger_time: u64 },
    TemporarilyUnavailable,
    Duplicate { duplicate_of: BlockIndex },
    GenericError { error_code: Nat, message: String },
}
```
