### Title
ICP Sent to SNS Swap Canister's Default Account Is Permanently Irrecoverable - (File: rs/sns/swap/src/swap.rs)

### Summary
The SNS Swap canister requires buyers to send ICP to a **principal-specific subaccount** and then call `refresh_buyer_token_e8s` to register participation. The only recovery mechanism, `error_refund_icp`, exclusively operates on principal-derived subaccounts and never on the swap canister's default (no-subaccount) ICP account. Any ICP transferred directly to the swap canister's default account — bypassing the subaccount-based flow — is permanently locked with no on-chain recovery path.

### Finding Description
The SNS Swap canister's participation flow is a two-step process:

1. The buyer sends ICP to `Account { owner: swap_canister_id, subaccount: Some(principal_to_subaccount(buyer)) }` on the ICP ledger.
2. The buyer calls `refresh_buyer_token_e8s`, which reads the balance of that specific subaccount and registers the participation. [1](#0-0) 

The recovery function `error_refund_icp` is designed to return ICP that was sent to a subaccount but never registered (or only partially accepted). It always constructs the source account as `Account { owner: self_canister_id, subaccount: Some(principal_to_subaccount(source_principal_id)) }`: [2](#0-1) 

The transfer-back call also always uses `Some(source_subaccount)` as the `from_subaccount`: [3](#0-2) 

There is no code path in `error_refund_icp`, `sweep_icp`, or any other swap canister method that reads from or transfers out of the swap canister's **default account** (`subaccount: None`). `sweep_icp` only iterates over `self.buyers`, which are populated exclusively by `refresh_buyer_token_e8s`: [4](#0-3) 

If a user sends ICP to `Account { owner: swap_canister_id, subaccount: None }` — the swap canister's default ICP account — those tokens are not tracked in `self.buyers`, cannot be recovered via `error_refund_icp` (which only checks subaccounts), and cannot be swept by `sweep_icp`. The swap canister has no `withdraw` or admin-rescue function for its default ICP account. The tokens are permanently locked.

The proto definition confirms `error_refund_icp` is scoped only to subaccount-based escrow: [5](#0-4) 

### Impact Explanation
Any ICP sent to the SNS Swap canister's default account is permanently irrecoverable. The swap canister holds no function — neither user-callable nor governance-callable — that can transfer ICP out of its default account. The loss is proportional to the amount sent. For a high-value SNS swap, this could represent a significant ledger conservation failure: ICP tokens exist on the ledger but are permanently inaccessible to any principal, including the swap canister's controllers.

### Likelihood Explanation
The SNS participation flow requires users to manually compute a subaccount (`principal_to_subaccount(buyer_principal)`) and send to it. A user who constructs a raw ICP ledger transfer (e.g., via `dfx`, a custom script, or a wallet that does not implement the SNS participation flow) may send to the swap canister's principal ID directly without a subaccount. This is analogous to the ERC20 direct-transfer mistake described in the reference report. The likelihood is low-to-medium: it requires a user error in account construction, but the ICP ledger's account model (owner + optional subaccount) makes this mistake plausible, especially for developers or users using generic transfer tooling.

### Recommendation
Add a recovery function (callable by the swap canister's controllers or by governance proposal) that can transfer ICP from the swap canister's default account to a specified destination. Alternatively, document and enforce at the canister level that the default account balance should always be zero, and add a periodic check or assertion. A minimal fix is to expose an admin-only `rescue_icp_from_default_account` method that transfers the default-account balance to a safe destination (e.g., the SNS governance treasury), analogous to the `rescue` function added in the referenced pull request 14.

### Proof of Concept
1. Obtain the SNS Swap canister ID (e.g., `swap_canister_id`).
2. Send ICP to `Account { owner: swap_canister_id, subaccount: None }` via the ICP ledger's `icrc1_transfer`.
3. After the swap reaches `Committed` or `Aborted` state, call `error_refund_icp` with any `source_principal_id`. The function will check `Account { owner: swap_canister_id, subaccount: Some(principal_to_subaccount(source_principal_id)) }` — which has zero balance — and return an error or transfer zero.
4. Call `sweep_icp`. It iterates only over `self.buyers` (registered via `refresh_buyer_token_e8s`) and never touches the default account.
5. The ICP in the default account remains permanently locked with no on-chain recovery path. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1152-1163)
```rust
        // Look for the token balance of the specified principal's subaccount on 'this' canister.
        let e8s = {
            let account = Account {
                owner: this_canister.get().0,
                subaccount: Some(principal_to_subaccount(&buyer)),
            };
            icp_ledger
                .account_balance(account)
                .await
                .map_err(|x| x.to_string())?
                .get_e8s()
        };
```

**File:** rs/sns/swap/src/swap.rs (L1925-1936)
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
```

**File:** rs/sns/swap/src/swap.rs (L1950-1969)
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
            // This buyer has participated in the swap, but all ICP
            // has already been disbursed, either back to the buyer
            // (aborted) or to the SNS Governance canister
            // (committed). Any ICP in this buyer's subaccount must
            // belong to the buyer.
        } else {
            // This buyer is not known to the swap canister. Any
            // balance in a subaccount belongs to the buyer.
        }
```

**File:** rs/sns/swap/src/swap.rs (L1971-1980)
```rust
        let source_subaccount = principal_to_subaccount(source_principal_id);

        // Figure out how much to send back to source_principal_id based on
        // what's left in the subaccount.
        let account_balance_result = icp_ledger
            .account_balance(Account {
                owner: self_canister_id.into(),
                subaccount: Some(source_subaccount),
            })
            .await;
```

**File:** rs/sns/swap/src/swap.rs (L1996-2004)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L2046-2070)
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
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L1227-1234)
```text
// Request a refund of tokens that were sent to the canister in
// error. The refund is always on the ICP ledger, from this canister's
// subaccount of the caller to the account of the caller.
message ErrorRefundIcpRequest {
  // Principal who originally sent the funds to us, and is now asking for any
  // unaccepted balance to be returned.
  ic_base_types.pb.v1.PrincipalId source_principal_id = 1;
}
```
