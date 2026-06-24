### Title
SNS Swap Canister Uses Hardcoded `DEFAULT_TRANSFER_FEE` in ICP Ledger Calls, Permanently Blocking User Fund Retrieval if Ledger Fee Changes - (`rs/sns/swap/src/swap.rs`)

### Summary

The SNS Swap canister's `sweep_icp` and `error_refund_icp` functions pass a compile-time constant `DEFAULT_TRANSFER_FEE` as the fee argument when calling the ICP ledger's `transfer_funds`. The ICP ledger enforces a strict equality check on the fee field. If the ICP ledger's transfer fee is ever changed via an NNS governance upgrade, every pending `sweep_icp` call will permanently fail with `BadFee`, preventing all SNS swap participants from recovering their ICP — either as refunds (aborted swap) or as committed funds routed to SNS governance (committed swap).

### Finding Description

In `rs/sns/swap/src/swap.rs`, the `sweep_icp` function iterates over all buyers and calls `transfer_helper` with the hardcoded constant `DEFAULT_TRANSFER_FEE`:

```rust
// rs/sns/swap/src/swap.rs  ~line 2113-2121
let result = icp_transferable_amount
    .transfer_helper(
        now_fn,
        DEFAULT_TRANSFER_FEE,          // ← compile-time constant, never queried from ledger
        Some(subaccount),
        &dst,
        icp_ledger,
    )
    .await;
``` [1](#0-0) 

Similarly, `error_refund_icp` hardcodes the same constant:

```rust
// rs/sns/swap/src/swap.rs  ~line 1991-2004
let amount_e8s = balance_e8s.saturating_sub(DEFAULT_TRANSFER_FEE.get_e8s());
let transfer_result = icp_ledger
    .transfer_funds(
        amount_e8s,
        DEFAULT_TRANSFER_FEE.get_e8s(),   // ← hardcoded
        Some(source_subaccount),
        dst,
        0,
    )
    .await;
``` [2](#0-1) 

The ICP ledger's `send` function enforces a **strict equality** check on the fee:

```rust
// rs/ledger_suite/icp/ledger/src/main.rs  ~line 233-238
let transfer_fee = LEDGER.read().unwrap().transfer_fee;
if fee != transfer_fee {
    return Err(TransferError::BadFee {
        expected_fee: transfer_fee,
    });
}
``` [3](#0-2) 

The ICRC-1 path in the ICP ledger applies the same check when a fee is explicitly provided:

```rust
// rs/ledger_suite/icp/ledger/src/main.rs  ~line 333-336
let expected_fee = LEDGER.read().unwrap().transfer_fee;
if fee.is_some() && fee.as_ref() != Some(&Nat::from(expected_fee.get_e8s())) {
    return Err(CoreTransferError::BadFee { expected_fee });
}
``` [4](#0-3) 

The swap canister never queries the current fee from the ledger before issuing transfers. `DEFAULT_TRANSFER_FEE` is a compile-time constant baked into the swap WASM. If the ICP ledger fee is changed (e.g., via an NNS upgrade proposal), every subsequent `sweep_icp` call will return `BadFee` for every buyer, and `finalize` will halt entirely:

```rust
// rs/sns/swap/src/swap.rs  ~line 2136
TransferResult::Failure(_) => {
    sweep_result.failure += 1;
}
``` [5](#0-4) 

The `finalize` function explicitly halts when `sweep_icp` does not complete fully:

> "Transferring ICP did not complete fully, some transfers were invalid or failed. Halting swap finalization" [6](#0-5) 

### Impact Explanation

All ICP deposited by buyers into an SNS swap is held in per-buyer subaccounts of the swap canister. The only mechanism to return these funds — either as refunds (aborted swap) or as committed ICP routed to SNS governance (committed swap) — is `sweep_icp`. If `sweep_icp` permanently fails due to a `BadFee` mismatch, buyer funds are locked in the swap canister with no recovery path. The `error_refund_icp` fallback path is equally broken because it uses the same hardcoded constant. This is a **ledger conservation bug**: tokens are permanently stranded in the swap canister's subaccounts.

### Likelihood Explanation

The ICP ledger fee is a governance-controlled parameter. An NNS proposal to upgrade the ICP ledger and change its transfer fee is a routine, legitimate governance action (the fee has historically been 10,000 e8s but is not immutable). Any such change — even a well-intentioned one — would immediately break all in-flight or future SNS swap finalizations. Because SNS swaps can remain in a finalizable state for extended periods, the window of exposure is non-trivial. No attacker capability is required; the condition arises from normal protocol governance.

### Recommendation

Replace the hardcoded `DEFAULT_TRANSFER_FEE` in `sweep_icp` and `error_refund_icp` with a dynamic query to the ICP ledger's `transfer_fee` endpoint before issuing transfers. Alternatively, use the ICRC-1 `icrc1_transfer` endpoint with `fee: None`, which instructs the ledger to apply the current fee automatically, eliminating the mismatch risk entirely. [7](#0-6) [8](#0-7) 

### Proof of Concept

1. An SNS swap is deployed and reaches `Committed` or `Aborted` lifecycle state with multiple buyers having deposited ICP.
2. An NNS governance proposal is passed that upgrades the ICP ledger and changes its `transfer_fee` from 10,000 e8s to any other value (e.g., 20,000 e8s).
3. Any caller invokes `finalize` (or `sweep_icp` directly) on the swap canister.
4. `sweep_icp` calls `transfer_helper(..., DEFAULT_TRANSFER_FEE, ...)` for each buyer, passing the old hardcoded fee of 10,000 e8s.
5. The ICP ledger rejects every transfer with `BadFee { expected_fee: 20_000 }`.
6. Every buyer's transfer is counted as `failure` in `SweepResult`.
7. `finalize` halts with the error message above; no SNS neurons are created, no ICP is returned to buyers, and no further state transitions are possible.
8. Buyers' ICP remains permanently locked in the swap canister's subaccounts with no on-chain recovery path.

### Citations

**File:** rs/sns/swap/src/swap.rs (L1925-2032)
```rust
    pub async fn error_refund_icp(
        &self,
        self_canister_id: CanisterId,
        request: &ErrorRefundIcpRequest,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> ErrorRefundIcpResponse {
        // Fail if the request is premature.
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
        }

        // Unpack request.
        let source_principal_id = match request {
            ErrorRefundIcpRequest {
                source_principal_id: Some(source_principal_id),
            } => source_principal_id,
            _ => {
                return ErrorRefundIcpResponse::new_invalid_request_error(format!(
                    "Invalid request. Must have source_principal_id. Request:\n{request:#?}",
                ));
            }
        };

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
            // This buyer has participated in the swap, but all ICP
            // has already been disbursed, either back to the buyer
            // (aborted) or to the SNS Governance canister
            // (committed). Any ICP in this buyer's subaccount must
            // belong to the buyer.
        } else {
            // This buyer is not known to the swap canister. Any
            // balance in a subaccount belongs to the buyer.
        }

        let source_subaccount = principal_to_subaccount(source_principal_id);

        // Figure out how much to send back to source_principal_id based on
        // what's left in the subaccount.
        let account_balance_result = icp_ledger
            .account_balance(Account {
                owner: self_canister_id.into(),
                subaccount: Some(source_subaccount),
            })
            .await;
        let balance_e8s = match account_balance_result {
            Ok(balance) => balance.get_e8s(),
            Err(err) => {
                return ErrorRefundIcpResponse::new_external_error(format!(
                    "Unable to get the balance for the subaccount of {source_principal_id}: {err:?}",
                ));
            }
        };

        // Make transfer.
        let amount_e8s = balance_e8s.saturating_sub(DEFAULT_TRANSFER_FEE.get_e8s());
        let dst = Account {
            owner: source_principal_id.0,
            subaccount: None,
        };
        let transfer_result = icp_ledger
            .transfer_funds(
                amount_e8s,
                DEFAULT_TRANSFER_FEE.get_e8s(),
                Some(source_subaccount),
                dst,
                0, // memo
            )
            .await;

        // Translate transfer result into return value.
        match transfer_result {
            Ok(block_height) => {
                log!(
                    INFO,
                    "Error refund - transferred {} ICP from subaccount {:#?} to {} at height {}",
                    amount_e8s,
                    source_subaccount,
                    dst,
                    block_height,
                );
                ErrorRefundIcpResponse::new_ok(block_height)
            }
            Err(err) => {
                log!(
                    ERROR,
                    "Error refund - failed to transfer {} from subaccount {:#?}: {}",
                    amount_e8s,
                    source_subaccount,
                    err,
                );
                ErrorRefundIcpResponse::new_external_error(format!(
                    "Transfer request failed: {err}",
                ))
            }
        }
    }
```

**File:** rs/sns/swap/src/swap.rs (L2046-2154)
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

            // Update the buyer state to indicate funds that have been successfully committed or refunded.
            if result.is_success() {
                // Record transfer fee
                icp_transferable_amount.transfer_fee_paid_e8s =
                    Some(DEFAULT_TRANSFER_FEE.get_e8s());
                // Record the amount minus transfer fee that was refunded or committed.
                let amount_transferred_e8s =
                    Some(icp_transferable_amount.amount_e8s - DEFAULT_TRANSFER_FEE.get_e8s());
                icp_transferable_amount.amount_transferred_e8s = amount_transferred_e8s;
            }
        }

        sweep_result
    }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L233-238)
```rust
        let transfer_fee = LEDGER.read().unwrap().transfer_fee;
        if fee != transfer_fee {
            return Err(TransferError::BadFee {
                expected_fee: transfer_fee,
            });
        }
```

**File:** rs/ledger_suite/icp/ledger/src/main.rs (L333-336)
```rust
        let expected_fee = LEDGER.read().unwrap().transfer_fee;
        if fee.is_some() && fee.as_ref() != Some(&Nat::from(expected_fee.get_e8s())) {
            return Err(CoreTransferError::BadFee { expected_fee });
        }
```

**File:** rs/sns/swap/tests/swap.rs (L2641-2645)
```rust
    assert_eq!(
        result.error_message,
        Some(String::from(
            "Transferring ICP did not complete fully, some transfers were invalid or failed. Halting swap finalization"
        ))
```
