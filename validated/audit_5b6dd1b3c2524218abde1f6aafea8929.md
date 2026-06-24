Audit Report

## Title
`sweep_icp` Invalid Buyer States Permanently Halt SNS Swap Finalization With No On-Chain Recovery - (`rs/sns/swap/src/swap.rs`)

## Summary
When `sweep_icp` encounters any buyer in a permanently `invalid` state (amount below transfer fee, or `BuyerState.icp == None`), `finalize_inner` permanently halts on every subsequent call with no on-chain recovery path. The `error_refund_icp` escape hatch is simultaneously blocked for these same buyers because their `transfer_success_timestamp_seconds` remains `0`. The result is permanently locked ICP and a permanently frozen SNS finalization pipeline requiring NNS governance intervention to resolve.

## Finding Description

**Path 1 — `AmountTooSmall`:** `sweep_icp` calls `transfer_helper` with `DEFAULT_TRANSFER_FEE`. If `amount_e8s <= DEFAULT_TRANSFER_FEE`, `TransferResult::AmountTooSmall` is returned and `sweep_result.invalid += 1` is set. [1](#0-0) 

This is reachable when `min_participant_icp_e8s` is misconfigured below `DEFAULT_TRANSFER_FEE` (10,000 e8s), since `refresh_buyer_tokens` accepts contributions ≥ `min_participant_icp_e8s` without enforcing a floor at `DEFAULT_TRANSFER_FEE`.

**Path 2 — `BuyerState.icp == None`:** If the `icp` field is absent (state corruption), `sweep_result.invalid += 1` is set. [2](#0-1) 

**Halting mechanism:** `is_successful_sweep` returns `false` whenever `invalid > 0`: [3](#0-2) 

`set_sweep_icp_result` then sets an error message on the response: [4](#0-3) 

`finalize_inner` immediately returns on any error message after `sweep_icp`, skipping SNS token distribution, governance mode transition, and dapp controller restoration: [5](#0-4) 

Because `invalid` entries are permanent by protocol definition — the proto explicitly states *"on this call and all future calls to finalize, this item will not be successful"* — every subsequent call to `finalize` will hit the same halt. [6](#0-5) 

**`error_refund_icp` blocked:** For `AmountTooSmall`, `transfer_success_timestamp_seconds` is never set (only updated on `result.is_success()`): [7](#0-6) 

`error_refund_icp` explicitly rejects any buyer whose `transfer_success_timestamp_seconds == 0`: [8](#0-7) 

This guard is intended to prevent double-recovery while ICP is in escrow, but it permanently blocks recovery for buyers whose ICP can never be swept via the normal mechanism.

## Impact Explanation

This is a **High** severity finding matching: *"Significant SNS security impact with concrete user or protocol harm."*

When triggered in `COMMITTED` state: all SNS tokens allocated to the swap remain locked in the Swap canister's SNS ledger account indefinitely; SNS governance is never set to Normal mode; dapp controllers are never transferred to SNS governance. All participants — not just the one invalid buyer — are affected. The affected buyer's ICP is permanently locked in their subaccount of the Swap canister with no on-chain recovery path. Recovery requires an NNS governance proposal to upgrade the Swap canister.

## Likelihood Explanation

**Low overall.** The `AmountTooSmall` path requires `min_participant_icp_e8s` to be set below `DEFAULT_TRANSFER_FEE` (10,000 e8s = 0.0001 ICP) in the SNS initialization proposal — a governance-level misconfiguration. No validation in the swap initialization enforces `min_participant_icp_e8s >= DEFAULT_TRANSFER_FEE`. The `BuyerState.icp == None` path requires state corruption (programmer error). Neither path requires a malicious actor; an honest governance mistake or a latent bug is sufficient. Once triggered, the impact is irreversible without external intervention.

## Recommendation

1. **Add parameter validation** during swap initialization to enforce `min_participant_icp_e8s > DEFAULT_TRANSFER_FEE`, making the `AmountTooSmall` path unreachable in production.
2. **Decouple `invalid` from finalization halting**: `is_successful_sweep` should only treat `failure > 0` and `global_failures > 0` as halt conditions. `invalid` entries should be logged and skipped, since they are irrecoverable by retrying and should not block the rest of finalization.
3. **Relax the `error_refund_icp` escrow guard** for buyers whose `amount_e8s <= DEFAULT_TRANSFER_FEE`, since their ICP can never be swept via the normal mechanism and the escrow protection is moot.

## Proof of Concept

1. Deploy an SNS Swap with `min_participant_icp_e8s = 5_000` (below `DEFAULT_TRANSFER_FEE = 10_000`).
2. Buyer A calls `refresh_buyer_tokens` contributing `5_000 e8s` — accepted, `BuyerState { amount_e8s: 5_000 }` created.
3. Buyer B contributes `100_000_000 e8s` (1 ICP) — accepted normally.
4. Swap reaches `COMMITTED` state.
5. Call `finalize`. `sweep_icp` runs: Buyer B → `success += 1`; Buyer A → `TransferResult::AmountTooSmall` → `invalid += 1`, `transfer_success_timestamp_seconds` stays `0`.
6. `set_sweep_icp_result` detects `invalid > 0` → sets error message. `finalize_inner` returns early. SNS tokens never distributed, governance never set to Normal.
7. All subsequent `finalize` calls repeat step 6 identically. Finalization is permanently blocked.
8. Buyer A calls `error_refund_icp` → rejected: *"ICP cannot be refunded as principal … has … ICP (e8s) in escrow"*.
9. Buyer B has no SNS tokens despite their ICP being swept. Only an NNS-approved canister upgrade can recover the state.

### Citations

**File:** rs/sns/swap/src/swap.rs (L1557-1561)
```rust
        finalize_swap_response
            .set_sweep_icp_result(self.sweep_icp(now_fn, environment.icp_ledger()).await);
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

**File:** rs/sns/swap/src/swap.rs (L2096-2110)
```rust
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
```

**File:** rs/sns/swap/src/swap.rs (L2127-2129)
```rust
                TransferResult::AmountTooSmall => {
                    sweep_result.invalid += 1;
                }
```

**File:** rs/sns/swap/src/swap.rs (L2141-2150)
```rust
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

**File:** rs/sns/swap/src/types.rs (L969-977)
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
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L896-899)
```text
  // Invalid means that on this call and all future calls to finalize,
  // this item will not be successful, and will need intervention to
  // succeed.
  uint32 invalid = 4;
```
