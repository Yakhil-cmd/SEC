### Title
Excess ICP Not Returned to Buyer After `refresh_buyer_tokens` - (File: rs/sns/swap/src/swap.rs)

### Summary
The SNS Swap canister's `refresh_buyer_token_e8s` function, exposed externally as `refresh_buyer_tokens`, accepts ICP from buyers but silently caps the accepted amount at per-participant and global swap limits without returning the excess ICP to the buyer during the same call. The excess ICP remains locked in the buyer's subaccount of the Swap canister until the swap closes (Committed or Aborted), at which point the buyer must separately call `error_refund_icp` to recover it. This is a direct analog to the missing `_transferRem` checks in the Solidity report.

### Finding Description

In `rs/sns/swap/src/swap.rs`, the `refresh_buyer_token_e8s` function reads the buyer's ICP balance from the ledger, then caps the accepted amount at two limits:

1. `max_increment_e8s` — the remaining global direct participation capacity (`available_direct_participation_e8s()`).
2. `max_participant_icp_e8s` — the per-participant cap. [1](#0-0) 

After capping, the function records only `new_balance_e8s` in the buyer's state and returns successfully — **without transferring the excess ICP back to the buyer**. The excess ICP (the difference between `e8s` on the ledger and `new_balance_e8s` accepted) remains stranded in the buyer's subaccount of the Swap canister on the ICP ledger.

The code itself acknowledges this gap with an open TODO: [2](#0-1) 

The only recovery path is `error_refund_icp`, which is gated behind the swap being in `Aborted` or `Committed` state: [3](#0-2) 

This means excess ICP is inaccessible to the buyer for the entire remaining duration of the open swap — which can be up to 90 days per the SNS swap parameters.

The externally callable endpoint is: [4](#0-3) 

### Impact Explanation

Any unprivileged ingress sender (a direct swap participant) who transfers more ICP than the per-participant cap (`max_participant_icp_e8s`) or more than the remaining global capacity (`available_direct_participation_e8s()`) will have their excess ICP locked in the Swap canister's subaccount for the duration of the swap. This is a **ledger conservation bug**: tokens are accepted into the canister's custody but not accounted for in the buyer's recorded participation, and not returned. The buyer cannot use those tokens elsewhere (e.g., staking, other swaps) until the swap closes. In a worst-case scenario where the swap runs for its maximum duration and the buyer sent a large excess, this represents a significant, time-locked loss of liquidity for the user.

### Likelihood Explanation

This is highly likely to be triggered in practice. The SNS swap UI and documentation encourage buyers to send the maximum they wish to participate with. When the swap is nearly full, any buyer who sends more than `available_direct_participation_e8s()` will have their excess locked. Similarly, any buyer who sends more than `max_participant_icp_e8s` will have the excess locked. Both conditions are routine during popular SNS launches. The attacker-controlled entry path is a standard ingress call to `refresh_buyer_tokens` by any principal — no special privileges required.

### Recommendation

Inside `refresh_buyer_token_e8s`, after computing `new_balance_e8s`, calculate the excess ICP (`e8s - new_balance_e8s`) and immediately initiate an ICP ledger transfer back to the buyer's principal account before returning. This mirrors the `_transferRem` pattern recommended in the original report. The existing `error_refund_icp` logic at lines 1971–2004 demonstrates the correct transfer pattern to reuse. The open TODO at line 1133 (`TODO(NNS1-1682): attempt to refund ICP that cannot be accepted`) confirms this is a known gap that has not been addressed. [5](#0-4) 

### Proof of Concept

1. A swap is open with `max_participant_icp_e8s = 5 ICP` and `available_direct_participation_e8s() = 3 ICP` (swap nearly full).
2. Buyer transfers 5 ICP to their subaccount of the Swap canister on the ICP ledger.
3. Buyer calls `refresh_buyer_tokens`.
4. `refresh_buyer_token_e8s` reads `e8s = 5 ICP`, computes `actual_increment_e8s = min(3, 5) = 3 ICP`, records `new_balance_e8s = 3 ICP`. [6](#0-5) 

5. The function returns `Ok(RefreshBuyerTokensResponse { icp_accepted_participation_e8s: 3 ICP, icp_ledger_account_balance_e8s: 5 ICP })` — the 2 ICP excess is **not returned**. [7](#0-6) 

6. The buyer's 2 ICP excess remains locked in `subaccount(swap_canister, buyer_principal)` on the ICP ledger.
7. The buyer calls `error_refund_icp` immediately — it fails with `"Error refunds can only be performed when the swap is ABORTED or COMMITTED"`. [8](#0-7) 

8. The buyer's 2 ICP is inaccessible until the swap closes, which may be weeks or months later.

### Citations

**File:** rs/sns/swap/src/swap.rs (L1133-1133)
```rust
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
```

**File:** rs/sns/swap/src/swap.rs (L1223-1237)
```rust
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
        if new_balance_e8s > max_participant_icp_e8s {
            log!(
                INFO,
                "Participant {} contributed {} e8s - the limit per participant is {}",
                buyer,
                new_balance_e8s,
                max_participant_icp_e8s
            );
        }

        // Limit the participation based on the maximum per participant.
        let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1308-1311)
```rust
        Ok(RefreshBuyerTokensResponse {
            icp_accepted_participation_e8s: new_balance_e8s,
            icp_ledger_account_balance_e8s: e8s,
        })
```

**File:** rs/sns/swap/src/swap.rs (L1925-2004)
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
```

**File:** rs/sns/swap/canister/canister.rs (L127-142)
```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    {
        Ok(r) => r,
        Err(msg) => panic!("{}", msg),
    }
```
