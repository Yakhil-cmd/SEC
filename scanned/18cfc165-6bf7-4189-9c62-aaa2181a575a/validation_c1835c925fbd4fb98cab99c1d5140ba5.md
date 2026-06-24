### Title
Missing `created_at_time` in SNS Swap Ledger Transfers Disables ICRC-1 Deduplication, Enabling Double-Spend on Response Loss - (`rs/nervous_system/clients/src/ledger_client.rs`)

---

### Summary

The `ICRC1Ledger::transfer_funds` implementation used by the SNS swap canister for all token movements during finalization explicitly sets `created_at_time: None`. This disables the ICRC-1 ledger's deduplication mechanism. If a transfer response is lost (e.g., during a subnet restart or canister upgrade mid-call) and the swap canister retries, the ledger executes the transfer a second time, enabling double-spending of ICP or SNS tokens.

---

### Finding Description

The production implementation of `ICRC1Ledger` for `LedgerCanister` in `rs/nervous_system/clients/src/ledger_client.rs` constructs every `TransferArg` with `created_at_time: None`:

```rust
let args = TransferArg {
    from_subaccount,
    to,
    fee: Some(Nat::from(fee_e8s)),
    created_at_time: None,   // ← deduplication disabled
    amount: Nat::from(amount_e8s),
    memo: Some(Memo::from(memo)),
};
``` [1](#0-0) 

This `transfer_funds` method is the sole ledger-call path used by the SNS swap canister during `sweep_icp` (which transfers ICP to SNS governance on commit, or back to buyers on abort) and `sweep_sns` (which distributes SNS tokens to buyer neuron accounts), both invoked from `finalize_inner`. [2](#0-1) 

The ICRC-1 standard specifies that when `created_at_time` is absent the ledger **must not** deduplicate the transaction. Consequently, two calls with identical `(from_subaccount, to, amount, memo)` — which is the case for every retry of the same buyer's sweep — are treated as independent transactions and both execute.

The swap canister tracks transfer completion via `transfer_success_timestamp_seconds` in `TransferableAmount`: [3](#0-2) 

However, this field is only set when the canister **receives** the success response. On the IC, an inter-canister call response can be permanently lost if the calling canister is stopped, upgraded, or if the subnet restarts while the call is in-flight. In that case `transfer_success_timestamp_seconds` remains `0`, the swap canister considers the transfer unfinished, and on the next call to `finalize` it retries — issuing a second ledger transfer that the ledger executes without objection.

The memo used in `sweep_icp` tests is `0` for every buyer transfer, confirming that no per-call uniqueness is injected to compensate for the absent timestamp: [4](#0-3) 

---

### Impact Explanation

| Scenario | Effect |
|---|---|
| `sweep_icp` in ABORTED state | Buyer receives a second ICP refund; swap canister is drained of ICP it does not own |
| `sweep_icp` in COMMITTED state | SNS governance canister receives a second ICP deposit; swap canister is over-drained |
| `sweep_sns` in COMMITTED state | Buyer neuron account receives a second SNS token allocation; SNS token supply conservation is violated |

All three cases break ledger conservation invariants. The double-spend amount equals the full participation amount of the affected buyer, which can be up to `max_participant_icp_e8s` (configurable, potentially millions of ICP e8s).

---

### Likelihood Explanation

The trigger condition — a lost inter-canister response — is a realistic, documented IC failure mode that occurs during subnet upgrades, canister upgrades, and subnet recovery. The SNS swap `finalize` function is explicitly designed to be called multiple times to retry partial failures:

> "The call to `finalize` does not happen automatically (i.e., on the canister heartbeat) so that there is a caller to respond to with potential errors." [5](#0-4) 

Any unprivileged principal can call `finalize` on a committed or aborted swap. If a subnet restart occurs between a successful ledger transfer and the swap canister recording `transfer_success_timestamp_seconds`, the next `finalize` call by any caller triggers the double-spend.

---

### Recommendation

Set `created_at_time` to the current IC time in `transfer_funds` so the ICRC-1 ledger can deduplicate retries within its 24-hour window:

```rust
let args = TransferArg {
    from_subaccount,
    to,
    fee: Some(Nat::from(fee_e8s)),
    created_at_time: Some(ic_cdk::api::time()),  // enable deduplication
    amount: Nat::from(amount_e8s),
    memo: Some(Memo::from(memo)),
};
```

The same fix should be applied to the `icrc2_approve` call in the same file: [6](#0-5) 

With `created_at_time` set, a retry of the same transfer (same from, to, amount, memo, and timestamp) will be rejected by the ledger as `Duplicate`, and the swap canister can treat that as a success and set `transfer_success_timestamp_seconds` accordingly.

---

### Proof of Concept

1. A SNS swap reaches COMMITTED state with buyer B contributing 100 ICP.
2. `finalize` is called; `sweep_icp` issues `transfer_funds(100 ICP - fee, fee, subaccount_B, sns_governance, 0)` with `created_at_time: None`.
3. The ICP ledger executes the transfer (block N). The response is lost due to a subnet restart.
4. `transfer_success_timestamp_seconds` for buyer B remains `0`.
5. Any caller invokes `finalize` again. `sweep_icp` sees `transfer_success_timestamp_seconds == 0`, considers the transfer incomplete, and issues the identical `transfer_funds` call again.
6. The ICP ledger, having no `created_at_time` to match against, executes the transfer a second time (block N+k), sending another 100 ICP to SNS governance from the swap canister's subaccount for buyer B.
7. The swap canister has now transferred 200 ICP for a 100 ICP participation, violating conservation. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/nervous_system/clients/src/ledger_client.rs (L38-66)
```rust
    async fn transfer_funds(
        &self,
        amount_e8s: u64,
        fee_e8s: u64,
        from_subaccount: Option<Subaccount>,
        to: Account,
        memo: u64,
    ) -> Result<BlockIndex, NervousSystemError> {
        let args = TransferArg {
            from_subaccount,
            to,
            fee: Some(Nat::from(fee_e8s)),
            created_at_time: None,
            amount: Nat::from(amount_e8s),
            memo: Some(Memo::from(memo)),
        };
        let res = self.client.transfer(args).await
            .map_err(|(code, msg)| {
                NervousSystemError::new_with_message(format!(
                    "Error calling method 'icrc1_transfer' of the icrc1 ledger canister. Code: {code:?}. Message: {msg}"
                ))
            })?;
        res.map_err(|err| {
            NervousSystemError::new_with_message(format!(
                "'icrc1_transfer' of the icrc1 ledger canister failed. Error: {err:?}"
            ))
        })
        .map(|n| n.0.to_u64().expect("nat does not fit into u64"))
    }
```

**File:** rs/nervous_system/clients/src/ledger_client.rs (L106-115)
```rust
        let args = ApproveArgs {
            spender,
            amount: Nat::from(amount),
            expires_at,
            fee: Some(Nat::from(fee)),
            from_subaccount,
            memo: None,
            created_at_time: None,
            expected_allowance: expected_allowance.map(Nat::from),
        };
```

**File:** rs/sns/swap/src/swap.rs (L1556-1598)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L2046-2069)
```rust
    pub async fn sweep_icp(
        &mut self,
        now_fn: fn(bool) -> u64,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> SweepResult {
        let lifecycle: Lifecycle = self.lifecycle();

        let init = match self.init_and_validate() {
            Ok(init) => init,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting sweep_icp(). State is missing or corrupted: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };

        // The following methods are safe to call since we validated Init in the above block
        let sns_governance = init.sns_governance_or_panic();

        let mut sweep_result = SweepResult::default();

```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L133-141)
```text
// Step 3a. (State COMMITTED). Tokens are allocated to participants at
// a single clearing price, i.e., the number of SNS tokens being
// offered divided by the total number of ICP tokens contributed to
// the swap. In this state, a call to `finalize` will create SNS
// neurons for each participant and transfer ICP to the SNS governance
// canister. The call to `finalize` does not happen automatically
// (i.e., on the canister heartbeat) so that there is a caller to
// respond to with potential errors.
//
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L637-654)
```text
message TransferableAmount {
  // The amount in e8s equivalent that the participant committed to the Swap,
  // which is held by the swap canister until the swap is committed or aborted.
  uint64 amount_e8s = 1;

  // When the transfer to refund or commit funds starts.
  uint64 transfer_start_timestamp_seconds = 2;

  // When the transfer to refund or commit succeeds.
  uint64 transfer_success_timestamp_seconds = 3;

  // The amount that was successfully transferred when swap commits or aborts
  // (minus fees).
  optional uint64 amount_transferred_e8s = 4;

  // The fee charged when transferring from the swap canister;
  optional uint64 transfer_fee_paid_e8s = 5;
}
```

**File:** rs/sns/swap/tests/swap.rs (L358-379)
```rust
                    LedgerExpect::TransferFunds(
                        2 * E8 - DEFAULT_TRANSFER_FEE.get_e8s(),
                        DEFAULT_TRANSFER_FEE.get_e8s(),
                        Some(principal_to_subaccount(&TEST_USER2_PRINCIPAL)),
                        Account {
                            owner: (*TEST_USER2_PRINCIPAL).into(),
                            subaccount: None,
                        },
                        0,
                        Ok(1066),
                    ),
                    LedgerExpect::TransferFunds(
                        2 * E8 - DEFAULT_TRANSFER_FEE.get_e8s(),
                        DEFAULT_TRANSFER_FEE.get_e8s(),
                        Some(principal_to_subaccount(&TEST_USER1_PRINCIPAL)),
                        Account {
                            owner: (*TEST_USER1_PRINCIPAL).into(),
                            subaccount: None,
                        },
                        0,
                        Ok(1067),
                    ),
```
