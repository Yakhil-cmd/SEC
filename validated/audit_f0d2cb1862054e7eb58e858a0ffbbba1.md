### Title
Rounding Dust SNS Tokens Permanently Stuck in Swap Canister After Finalization - (File: `rs/sns/swap/src/swap.rs`)

### Summary
After a committed SNS swap is finalized, integer-division rounding in `Swap::scale()` causes the sum of all per-buyer SNS token allocations to be strictly less than `params.sns_token_e8s`. The difference (rounding dust) remains in the swap canister's own SNS ledger account indefinitely. No function in the swap canister can recover these tokens: `error_refund_icp` handles only ICP, and there is no analogous `error_refund_sns` path.

### Finding Description

During `create_sns_neuron_recipes`, each buyer's SNS allocation is computed as:

```rust
let amount_sns_e8s = Swap::scale(
    buyer_state.amount_icp_e8s(),
    sns_being_offered_e8s,
    total_participant_icp_e8s,
);
``` [1](#0-0) 

`Swap::scale` performs integer (floor) division: `(buyer_icp × sns_total) / icp_total`. Summing this over all buyers yields a value that is **at most** `sns_being_offered_e8s`, with the shortfall being rounding dust. The same rounding occurs for Neurons' Fund participants at line 920–924. [2](#0-1) 

After `finalize_inner` calls `sweep_sns`, every neuron recipe's `amount_e8s` is transferred out of the swap canister's default SNS ledger account: [3](#0-2) 

The rounding dust that was never assigned to any recipe remains in `Account { owner: swap_canister_id, subaccount: None }` on the SNS ledger. The only post-finalization refund path is `error_refund_icp`, which exclusively transfers ICP from a buyer's ICP subaccount: [4](#0-3) 

There is no `error_refund_sns` or equivalent sweep-to-treasury step for leftover SNS tokens. The swap canister exposes no method to transfer its own SNS ledger balance after finalization is complete.

Additionally, the swap canister is loaded with exactly `initial_swap_amount_e8s` SNS tokens at genesis: [5](#0-4) 

If the actual SNS ledger balance at the time `open` is called exceeds `params.sns_token_e8s` (e.g., due to a direct transfer to the swap canister's account before or during the swap), that surplus is also permanently unrecoverable, mirroring the `skim()` scenario in the original report.

### Impact Explanation

A small but non-zero amount of SNS tokens is permanently locked in the swap canister's SNS ledger account after every committed swap. The tokens cannot be burned, transferred to the SNS treasury, or returned to any participant. Over many SNS launches the aggregate loss grows. The swap canister is controlled by SNS root post-finalization, so recovery requires a governance upgrade proposal—there is no built-in sweep mechanism.

### Likelihood Explanation

This occurs in **every** committed SNS swap where the ICP-to-SNS ratio does not divide evenly across all participants, which is the common case. The entry path requires only that a swap reaches the `Committed` lifecycle state and `finalize` is called—both are normal, unprivileged operations triggered by any caller or by the canister's own heartbeat (`should_auto_finalize`). [6](#0-5) 

### Recommendation

After `sweep_sns` completes successfully, transfer any remaining balance in the swap canister's default SNS ledger account to the SNS governance treasury account (the same destination used for committed ICP). This mirrors the recommendation in the original report to sweep leftover funds to a trusted address. Concretely, add a `sweep_sns_remainder` step at the end of `finalize_inner` that queries the swap canister's SNS balance and, if non-zero, issues a final transfer to `Account { owner: sns_governance, subaccount: None }`.

### Proof of Concept

Consider a swap with `sns_token_e8s = 10` and two buyers contributing `3` ICP and `4` ICP respectively out of `7` ICP total:

- Buyer A: `scale(3, 10, 7) = 30/7 = 4` SNS tokens
- Buyer B: `scale(4, 10, 7) = 40/7 = 5` SNS tokens
- Total distributed: `4 + 5 = 9` SNS tokens
- Dust stuck in swap canister: `10 - 9 = 1` SNS token (10% of the offered supply in this example)

After `sweep_sns` transfers 9 tokens out, the swap canister's SNS ledger account retains 1 token. No subsequent call to any swap canister method can move it. The `error_refund_icp` function at `rs/sns/swap/src/swap.rs:1925` only operates on ICP subaccounts, not the SNS ledger balance. [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L157-188)
```rust
impl NeuronBasketConstructionParameters {
    /// Chops `total_amount_e8s` into `self.count` pieces. Each gets doled out
    /// every `self.dissolve_delay_seconds`, starting from 0.
    ///
    /// # Arguments
    /// * `total_amount_e8s` - The total amount of tokens (in e8s) to be chopped up.
    fn generate_vesting_schedule(
        &self,
        total_amount_e8s: u64,
    ) -> Result<Vec<ScheduledVestingEvent>, String> {
        if self.count == 0 {
            return Err(
                "NeuronBasketConstructionParameters.count must be greater than zero".to_string(),
            );
        }

        let dissolve_delay_seconds_list = (0..(self.count))
            .map(|i| i * self.dissolve_delay_interval_seconds)
            .collect::<Vec<u64>>();

        let chunks_e8s = apportion_approximately_equally(total_amount_e8s, self.count)?;
        Ok(dissolve_delay_seconds_list
            .into_iter()
            .zip(chunks_e8s)
            .map(
                |(dissolve_delay_seconds, amount_e8s)| ScheduledVestingEvent {
                    dissolve_delay_seconds,
                    amount_e8s,
                },
            )
            .collect())
    }
```

**File:** rs/sns/swap/src/swap.rs (L848-852)
```rust
            let amount_sns_e8s = Swap::scale(
                buyer_state.amount_icp_e8s(),
                sns_being_offered_e8s,
                total_participant_icp_e8s,
            );
```

**File:** rs/sns/swap/src/swap.rs (L920-924)
```rust
                    let amount_sns_e8s = Swap::scale(
                        neurons_fund_neuron.amount_icp_e8s,
                        sns_being_offered_e8s,
                        total_participant_icp_e8s,
                    );
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

**File:** rs/sns/swap/src/swap.rs (L1906-1926)
```rust
    /// Requests a refund of ICP tokens transferred to the Swap
    /// canister that was either never notified (via the
    /// refresh_buyer_tokens Candid method), or not fully accepted (by
    /// refresh_buyer_tokens).
    ///
    /// This method makes no changes (and instead panics) unless
    /// finalization has completed successfully (see the finalize
    /// method), which can only happen after self has entered the
    /// Aborted or Committed state.
    ///
    /// The entire balance in `subaccount(swap_canister, P)` is
    /// transferred to request.principal_id (minus the transfer fee,
    /// of course).
    ///
    /// This method is secure because it only transfers tokens from a
    /// principal's subaccount (of the Swap canister) to the
    /// principal's own account, i.e., the tokens were held in escrow
    /// for the principal (buyer) before the call and are returned to
    /// the same principal.
    pub async fn error_refund_icp(
        &self,
```

**File:** rs/sns/swap/src/swap.rs (L2266-2274)
```rust
            let result = sns_transferable_amount
                .transfer_helper(
                    now_fn,
                    sns_transaction_fee_tokens,
                    /* src_subaccount= */ None,
                    &dst,
                    sns_ledger,
                )
                .await;
```

**File:** rs/sns/init/src/distributions.rs (L314-319)
```rust
        let swap_canister_account = Account {
            owner: sns_canister_ids.swap.0,
            subaccount: None,
        };
        let initial_swap_amount_tokens = Tokens::from_e8s(swap.initial_swap_amount_e8s);
        accounts.insert(swap_canister_account, initial_swap_amount_tokens);
```
