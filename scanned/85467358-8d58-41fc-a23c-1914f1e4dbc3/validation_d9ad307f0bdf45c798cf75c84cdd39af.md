### Title
Persistent Swap Finalization DOS via `invalid` BuyerState in `sweep_icp` — (`rs/sns/swap/src/swap.rs`, `rs/sns/swap/src/types.rs`)

### Summary

The SNS Swap canister's `finalize_swap` / `sweep_icp` pipeline can be permanently blocked (DOS) when any buyer's `BuyerState` produces an `invalid` or `failure` result during `sweep_icp`. Because `is_successful_sweep` treats both `invalid > 0` and `failure > 0` as fatal, and because the `invalid` path (e.g., `amount_e8s < DEFAULT_TRANSFER_FEE`) is **not retryable by design**, the entire finalization pipeline halts indefinitely. A malicious participant can deliberately engineer this condition to prevent the swap from completing, keeping ICP locked in the Swap canister and preventing SNS governance from being set to normal mode.

### Finding Description

The `sweep_icp` function in `rs/sns/swap/src/swap.rs` iterates over all buyers and calls `transfer_helper` for each. The `transfer_helper` in `rs/sns/swap/src/types.rs` returns `TransferResult::AmountTooSmall` when `amount_e8s <= fee`, which `sweep_icp` maps to `sweep_result.invalid += 1`. [1](#0-0) 

The `is_successful_sweep` check treats any nonzero `invalid`, `failure`, or `global_failures` as a fatal error: [2](#0-1) 

`set_sweep_icp_result` then sets an error message that halts all subsequent finalization steps: [3](#0-2) 

`finalize_inner` checks `has_error_message()` after `sweep_icp` and returns early, skipping `settle_neurons_fund_participation`, `sweep_sns`, `claim_swap_neurons`, and `set_sns_governance_to_normal_mode`: [4](#0-3) 

The `invalid` path in `sweep_icp` is explicitly documented as requiring "manual intervention via an upgrade to correct": [5](#0-4) 

The attack vector: a participant calls `refresh_buyer_tokens` to register a participation amount that is exactly `DEFAULT_TRANSFER_FEE` e8s (or less, if the ledger fee changes after participation). The ICP Ledger enforces a minimum transfer fee of 10,000 e8s, and `refresh_buyer_tokens` enforces `min_participant_icp_e8s`, but the fee is subtracted at sweep time. If `amount_e8s == DEFAULT_TRANSFER_FEE`, the net transfer amount is 0, which is caught as `AmountTooSmall` → `invalid`. This permanently blocks finalization.

Additionally, a transient ledger failure (e.g., the ICP ledger being upgraded mid-sweep) produces `failure > 0`, which also halts finalization. While `failure` is described as retryable, the `invalid` case is not — and both block the pipeline identically.

### Impact Explanation

- **Committed swap**: SNS governance is never set to normal mode, SNS tokens are never distributed to buyers, and ICP is never transferred to the SNS treasury. The SNS remains in pre-initialization mode indefinitely.
- **Aborted swap**: ICP is never returned to buyers. Funds are locked in the Swap canister's subaccounts.
- In both cases, the dapp controllers are never restored, and the SNS ecosystem is left in a broken state requiring an NNS-level canister upgrade to recover.

The `invalid` case is explicitly acknowledged as requiring "manual intervention via an upgrade," confirming the DOS is not self-healing. [6](#0-5) 

### Likelihood Explanation

The `invalid` path is reachable if:
1. A participant contributes exactly `min_participant_icp_e8s` ICP, and the ledger fee is subsequently raised to equal or exceed that amount (governance-controlled parameter change).
2. A participant contributes an amount that, after the ICP ledger deducts its fee, leaves exactly 0 net e8s — this is possible if `min_participant_icp_e8s` is set to `DEFAULT_TRANSFER_FEE` (10,000 e8s) in the SNS parameters.
3. A malicious SNS creator sets `min_participant_icp_e8s = DEFAULT_TRANSFER_FEE` and participates themselves, guaranteeing an `invalid` entry at sweep time.

The `failure` path (transient ledger error) is lower severity since it is retryable, but the `invalid` path is a permanent DOS requiring an upgrade.

Entry path: unprivileged ingress caller → `refresh_buyer_tokens` (open state) → `finalize_swap` (terminal state) → `sweep_icp` → `AmountTooSmall` → `invalid` → finalization permanently halted. [7](#0-6) 

### Recommendation

1. **Separate `invalid` from blocking**: `is_successful_sweep` should not treat `invalid > 0` as a fatal error that halts the entire pipeline. Instead, `invalid` entries should be skipped (logged) and the sweep should continue with remaining buyers. Only `failure > 0` (transient errors) should halt and allow retry.

2. **Enforce minimum participation above fee at registration time**: `refresh_buyer_tokens` should enforce `amount_e8s > DEFAULT_TRANSFER_FEE`, not just `amount_e8s >= min_participant_icp_e8s`, so that no buyer can ever enter the `AmountTooSmall` path at sweep time.

3. **Pull-over-push for failed transfers**: For buyers whose transfers fail permanently, record the failure in state and allow them to claim their ICP via a separate `error_refund_icp` call rather than blocking the entire sweep. [2](#0-1) 

### Proof of Concept

1. Deploy an SNS with `min_participant_icp_e8s = 10_000` (equal to `DEFAULT_TRANSFER_FEE`).
2. Attacker calls `refresh_buyer_tokens` contributing exactly 10,000 e8s ICP.
3. Swap reaches terminal state (Committed or Aborted).
4. `finalize_swap` is called → `sweep_icp` iterates buyers → attacker's `BuyerState` has `amount_e8s = 10_000`, `fee = 10_000` → `transfer_helper` returns `AmountTooSmall` → `sweep_result.invalid = 1`.
5. `set_sweep_icp_result` sees `invalid > 0` → sets `error_message` → `finalize_inner` returns early.
6. All subsequent finalization steps (`settle_neurons_fund_participation`, `sweep_sns`, `claim_swap_neurons`, `set_sns_governance_to_normal_mode`) are never executed.
7. The SNS is permanently stuck. Recovery requires an NNS-level upgrade of the Swap canister. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/swap/src/types.rs (L612-619)
```rust
        let amount = Tokens::from_e8s(self.amount_e8s);
        if amount <= fee {
            // Skip: amount too small...
            return TransferResult::AmountTooSmall;
        }
        if self.transfer_start_timestamp_seconds > 0 {
            // Operation in progress...
            return TransferResult::AlreadyStarted;
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

**File:** rs/sns/swap/src/types.rs (L968-978)
```rust
impl SweepResult {
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

**File:** rs/sns/swap/src/swap.rs (L1556-1561)
```rust
        // Transfer the ICP tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }
```

**File:** rs/sns/swap/src/swap.rs (L2070-2080)
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
```

**File:** rs/sns/swap/src/swap.rs (L2113-2139)
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
            }
```
