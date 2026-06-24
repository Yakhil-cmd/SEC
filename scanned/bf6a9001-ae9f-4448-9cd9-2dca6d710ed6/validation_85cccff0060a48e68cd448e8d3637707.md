### Title
Unprivileged Caller Can Force-Register Any Principal as SNS Swap Buyer via `refresh_buyer_tokens` — (`rs/sns/swap/canister/canister.rs`)

---

### Summary

The `refresh_buyer_tokens` canister endpoint accepts an arbitrary `buyer` field in its request without verifying it matches the caller. Any unprivileged principal can call this endpoint with a victim's principal ID, causing the victim to be registered as a swap buyer. This locks the victim's ICP in escrow and, if the swap commits, permanently converts their ICP into unwanted SNS neurons.

---

### Finding Description

The canister endpoint at `rs/sns/swap/canister/canister.rs` lines 130–133 contains the following logic:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // no caller == buyer check
};
``` [1](#0-0) 

When `arg.buyer` is non-empty, the parsed principal is used directly as the buyer identity with no assertion that it equals `caller_principal_id()`. The `RefreshBuyerTokensRequest` proto explicitly documents `buyer` as "If not specified, the caller is used," implying the field was intended as a convenience override for the caller themselves — but the implementation imposes no such restriction. [2](#0-1) 

The downstream `refresh_buyer_token_e8s` function then queries the ICP ledger balance of the victim's subaccount (`principal_to_subaccount(&buyer)`) and, if it meets `min_participant_icp_e8s`, inserts a `BuyerState` entry for the victim: [3](#0-2) [4](#0-3) 

The `BuyerState` is initialized with `transfer_success_timestamp_seconds = 0`: [5](#0-4) 

After the swap closes (COMMITTED or ABORTED), the victim calls `error_refund_icp` to recover their accidentally-sent ICP. However, `error_refund_icp` checks for a `BuyerState` entry with `transfer_success_timestamp_seconds == 0` and returns a precondition error, blocking the refund: [6](#0-5) 

The victim's ICP remains locked until `sweep_icp` completes during finalization.

**Regarding the `confirmation_text` mitigation**: `validate_confirmation_text` compares the caller-supplied text against the value stored in `Init`. However, the confirmation text is a publicly readable field (accessible via `get_init`), so an attacker can trivially read and supply it. [7](#0-6) 

---

### Impact Explanation

**If the swap COMMITS**: After `sweep_icp` runs, the victim's ICP is transferred to SNS governance. The victim receives SNS neurons they never consented to acquire. Their ICP is permanently converted into illiquid governance tokens.

**If the swap ABORTS**: After `sweep_icp` runs, the victim's ICP is returned to them. The harm is temporary lock-up and forced participation in the swap's accounting.

In both cases, the victim is denied the ability to use `error_refund_icp` to self-rescue their accidentally-sent ICP during the window between swap close and `sweep_icp` completion.

---

### Likelihood Explanation

- The attack requires only that the victim has accidentally sent ICP to their swap subaccount (a realistic user error, especially given the subaccount-based participation model).
- The attacker needs no special privileges — only the ability to send an ingress message to the swap canister.
- The confirmation text (if set) is publicly readable from `get_init`, so it provides no real barrier.
- The attack window is the entire OPEN lifecycle of the swap.

---

### Recommendation

In the canister endpoint, enforce that the `buyer` field, if provided, must equal the caller:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    if specified != caller_principal_id() {
        panic!("buyer field must match the caller");
    }
    specified
};
``` [8](#0-7) 

Alternatively, remove the `buyer` field entirely from `RefreshBuyerTokensRequest` and always use `caller_principal_id()`.

---

### Proof of Concept

1. Victim accidentally sends `min_participant_icp_e8s` ICP to `swap_subaccount(victim_principal)` on the ICP ledger.
2. Attacker (any principal) calls:
   ```
   refresh_buyer_tokens(RefreshBuyerTokensRequest {
       buyer: victim_principal.to_string(),
       confirmation_text: <read from get_init>,
   })
   ```
3. The swap canister queries the victim's subaccount balance, finds sufficient ICP, and inserts `BuyerState { icp: TransferableAmount { amount_e8s: X, transfer_success_timestamp_seconds: 0, ... } }` for the victim.
4. Swap closes (COMMITTED). Victim calls `error_refund_icp` — blocked by the `transfer_success_timestamp_seconds == 0` guard.
5. `sweep_icp` runs, transferring victim's ICP to SNS governance. Victim receives unwanted SNS neurons. [9](#0-8)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L128-143)
```rust
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
}
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L1117-1126)
```rust
pub struct RefreshBuyerTokensRequest {
    /// If not specified, the caller is used.
    #[prost(string, tag = "1")]
    pub buyer: ::prost::alloc::string::String,
    /// To accept the swap participation confirmation, a participant should send
    /// the confirmation text via refresh_buyer_tokens, matching the text set
    /// during SNS initialization.
    #[prost(string, optional, tag = "2")]
    pub confirmation_text: ::core::option::Option<::prost::alloc::string::String>,
}
```

**File:** rs/sns/swap/src/swap.rs (L363-384)
```rust
        pub fn validate_confirmation_text(
            &self,
            confirmation_text: Option<String>,
        ) -> Result<(), String> {
            match (
                self.init_or_panic().confirmation_text.as_ref(),
                confirmation_text,
            ) {
                (Some(expected_text), Some(text)) => {
                    if &text != expected_text {
                        Err("The value of `confirmation_text` does not match the value provided in SNS init payload.".to_string())
                    } else {
                        Ok(())
                    }
                }
                (Some(_), None) => Err("No value provided for `confirmation_text`.".to_string()),
                (None, Some(_)) => {
                    Err("Found a value for `confirmation_text`, expected none.".to_string())
                }
                (None, None) => Ok(()),
            }
        }
```

**File:** rs/sns/swap/src/swap.rs (L1153-1163)
```rust
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

**File:** rs/sns/swap/src/swap.rs (L1285-1288)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1925-1960)
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
```

**File:** rs/sns/swap/src/types.rs (L540-551)
```rust
    pub fn new(amount_icp_e8s: u64) -> Self {
        Self {
            icp: Some(TransferableAmount {
                amount_e8s: amount_icp_e8s,
                transfer_start_timestamp_seconds: 0,
                transfer_success_timestamp_seconds: 0,
                amount_transferred_e8s: Some(0),
                transfer_fee_paid_e8s: Some(0),
            }),
            has_created_neuron_recipes: Some(false),
        }
    }
```
