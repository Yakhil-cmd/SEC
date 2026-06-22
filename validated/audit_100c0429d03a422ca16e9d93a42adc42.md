### Title
Unprivileged Caller Can Force Any Principal With ICP in Swap Subaccount Into SNS Token Swap Participation - (File: rs/sns/swap/canister/canister.rs)

---

### Summary

The `refresh_buyer_tokens` update method on the SNS Swap canister accepts an arbitrary `buyer` principal in its request payload and performs no check that the caller equals the specified buyer. Any unprivileged ingress sender can call this method with a victim's principal, causing the victim's ICP (already sitting in the swap's subaccount derived from the victim's principal) to be irrevocably committed as swap participation — locking the victim's funds until the swap finalizes and potentially exchanging their ICP for low-value SNS tokens without their consent.

---

### Finding Description

`refresh_buyer_tokens` in `rs/sns/swap/canister/canister.rs` resolves the effective buyer principal from the caller-supplied `arg.buyer` string field with no authorization check:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // ← any principal accepted
};
``` [1](#0-0) 

The resolved principal `p` is then passed directly to `refresh_buyer_token_e8s`, which:

1. Reads the ICP balance held in the swap canister's subaccount derived from `buyer` (i.e., `principal_to_subaccount(&buyer)`).
2. If the balance meets the minimum participation threshold, registers `buyer` as a committed participant and records the ICP amount in `self.buyers`. [2](#0-1) [3](#0-2) 

The `confirmation_text` field is not a meaningful mitigation: it is stored in the swap's `Init` payload, which is publicly readable via the `get_init` query endpoint. An attacker reads the text and includes it verbatim in the call. [4](#0-3) 

Once committed, the victim's ICP cannot be reclaimed via `error_refund_icp` while the swap is still `OPEN`; that endpoint is gated on the swap being `ABORTED` or `COMMITTED`. [5](#0-4) 

The public Candid interface explicitly exposes `refresh_buyer_tokens` as an open update call with no caller restriction: [6](#0-5) 

The proto definition confirms `buyer` is an optional free-text field defaulting to the caller only when empty: [7](#0-6) 

---

### Impact Explanation

A victim who has transferred ICP to the swap canister's subaccount (the required first step of participation, e.g., while evaluating whether to participate, or mid-flow before deciding to abort) can be forcibly enrolled as a buyer by any third party. Consequences:

- The victim's ICP is locked in the swap until finalization (no refund path while `OPEN`).
- If the swap commits successfully, the victim receives SNS tokens — potentially of negligible market value — in exchange for their ICP, with no consent.
- If the swap aborts, the victim eventually recovers their ICP minus transfer fees, but only after the swap closes, which can be days away.

This is a **ledger conservation / governance authorization bug**: ICP belonging to a principal is committed to a financial outcome without that principal's authorization.

---

### Likelihood Explanation

- The attack requires only a standard ingress call from any unprivileged principal — no special role, key, or majority is needed.
- The victim's precondition (ICP sitting in the swap subaccount) is a normal intermediate state that every participant passes through; the window is non-trivial.
- The confirmation text (when present) is publicly readable from `get_init`, so it provides no barrier.
- The attack is cheap (one update call) and repeatable across all open SNS swaps simultaneously.

---

### Recommendation

In `refresh_buyer_tokens`, reject any request where `arg.buyer` is non-empty and does not match `caller_principal_id()`:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let requested = PrincipalId::from_str(&arg.buyer).unwrap();
        if requested != caller {
            panic!("caller {} is not authorized to refresh tokens on behalf of {}", caller, requested);
        }
        requested
    };
    // ...
}
```

Alternatively, remove the `buyer` field entirely and always use `caller_principal_id()`, since the only legitimate use case is a principal notifying the swap of their own transfer.

---

### Proof of Concept

**Setup:**
- SNS swap is `OPEN`, no `confirmation_text` (or attacker reads it from `get_init`).
- Victim `V` transfers 10 ICP to the swap canister's subaccount `principal_to_subaccount(V)` on the ICP ledger, intending to evaluate participation.

**Attack (single ingress call from attacker `A`):**

```
dfx canister call <swap_canister_id> refresh_buyer_tokens '(
  record {
    buyer = "<victim_principal_text_id>";
    confirmation_text = null
  }
)'
``` [8](#0-7) 

**Result:**
- `refresh_buyer_token_e8s` reads the balance of `principal_to_subaccount(V)` on the ICP ledger, finds 10 ICP, and registers `V` as a buyer with `amount_icp_e8s = 10_0000_0000`.
- `V` is now a committed participant. Their ICP is locked until the swap finalizes.
- `V` cannot call `error_refund_icp` while the swap is `OPEN`.
- If the swap commits, `V` receives SNS tokens they never agreed to purchase. [2](#0-1) [9](#0-8)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L127-143)
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

**File:** rs/sns/swap/src/swap.rs (L1274-1291)
```rust
        // Append to a new buyer to the BUYERS_LIST_INDEX
        let is_preexisting_buyer = self.buyers.contains_key(&buyer.to_string());
        if !is_preexisting_buyer {
            insert_buyer_into_buyers_list_index(buyer)
                .map_err(|grow_failed| {
                    format!(
                        "Failed to add buyer {buyer} to state, the canister's stable memory could not grow: {grow_failed}"
                    )
                })?;
        }

        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
        // We compute the current participation amounts once and store the result in Swap's state,
        // for efficiency reasons.
        self.update_total_participation_amounts();
```

**File:** rs/sns/swap/src/swap.rs (L1931-1936)
```rust
        // Fail if the request is premature.
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
        }
```

**File:** rs/sns/swap/canister/swap.did (L498-500)
```text
  refresh_buyer_tokens : (RefreshBuyerTokensRequest) -> (
      RefreshBuyerTokensResponse,
    );
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L843-851)
```text
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;

  // To accept the swap participation confirmation, a participant should send
  // the confirmation text via refresh_buyer_tokens, matching the text set
  // during SNS initialization.
  optional string confirmation_text = 2;
}
```
