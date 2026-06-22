### Title
Unrestricted `refresh_buyer_tokens` Allows Any Caller to Register Swap Participation on Behalf of Any Buyer, Bypassing `confirmation_text` Consent — (File: `rs/sns/swap/canister/canister.rs`)

---

### Summary

The SNS Swap canister's `refresh_buyer_tokens` update endpoint accepts an arbitrary `buyer` principal in its request body with no check that the caller equals that buyer. When an SNS is configured with a `confirmation_text` (a legal/regulatory consent gate), any unprivileged ingress sender can supply the victim's principal and the publicly readable `confirmation_text`, committing the victim's ICP to the swap without the victim ever explicitly agreeing to the terms. This is the direct IC analog of H-27: an unauthenticated "vest on behalf of" entry point that lets an attacker act on another user's account.

---

### Finding Description

**Entry point — `rs/sns/swap/canister/canister.rs` lines 127–143:**

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← no caller == p check
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, ...)
        .await
``` [1](#0-0) 

When `arg.buyer` is non-empty the resolved principal `p` is taken verbatim from attacker-controlled input. `caller_principal_id()` is never compared to `p`.

**Confirmation-text validation — `rs/sns/swap/src/swap.rs` lines 363–384:**

```rust
pub fn validate_confirmation_text(
    &self,
    confirmation_text: Option<String>,
) -> Result<(), String> {
    match (self.init_or_panic().confirmation_text.as_ref(), confirmation_text) {
        (Some(expected_text), Some(text)) => {
            if &text != expected_text { Err(...) } else { Ok(()) }
        }
        ...
    }
}
``` [2](#0-1) 

The expected text is stored in `Init.confirmation_text`, which is returned to anyone via the unauthenticated `get_init` query. An attacker reads it, then supplies it verbatim in their call.

**Proto definition confirming the `buyer` field is caller-supplied:** [3](#0-2) 

The comment "If not specified, the caller is used" makes the design intent clear — the field was meant as a convenience for self-calls, not as an open delegation mechanism.

**Participation is then persisted in `buyers` map — `rs/sns/swap/src/swap.rs` lines 1200–1312:**

Once `refresh_buyer_token_e8s` succeeds, the buyer's entry is written into `self.buyers`. There is no subsequent mechanism for the victim to undo this registration while the swap is open. [4](#0-3) 

---

### Impact Explanation

1. **Consent bypass**: The `confirmation_text` is the SNS creator's mechanism to obtain explicit legal/regulatory agreement (e.g., "I am not a US person", KYC attestations). An attacker commits the victim's ICP to the swap while forging that agreement.
2. **Irreversible during swap lifetime**: Once `refresh_buyer_tokens` succeeds, `error_refund_icp` is unavailable to the victim (it only applies when `refresh_buyer_tokens` itself failed). The victim's ICP remains locked until the swap closes.
3. **Forced financial outcome**: If the swap commits, the victim receives SNS tokens they never chose to acquire under terms they never accepted. If the swap aborts, the ICP is eventually returned, but the victim's funds were held without consent for the swap duration.
4. **SNS creator's invariant broken**: The `confirmation_text` feature exists precisely to gate participation; this bypass makes it a no-op for any buyer who has already transferred ICP to their subaccount.

---

### Likelihood Explanation

- No privileges required — any ingress sender can call `refresh_buyer_tokens`.
- The `confirmation_text` is publicly readable via the unauthenticated `get_init` query endpoint.
- The victim's principal ID is observable on-chain (e.g., from prior ledger transfers to the swap subaccount).
- The attack requires only that the victim has already transferred ICP to their swap subaccount (a normal first step in the participation flow), making front-running straightforward.

---

### Recommendation

Enforce that when `arg.buyer` is non-empty, the caller must equal the resolved buyer principal:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let p = PrincipalId::from_str(&arg.buyer).unwrap();
    if p != caller_principal_id() {
        panic!("Caller {} is not authorized to refresh tokens on behalf of {}", caller_principal_id(), p);
    }
    p
};
```

Alternatively, remove the `buyer` field entirely and always derive the buyer from `caller_principal_id()`, consistent with how `new_sale_ticket` and `notify_payment_failure` are implemented. [5](#0-4) [6](#0-5) 

---

### Proof of Concept

1. An SNS swap is deployed with `confirmation_text = "I confirm I am not a US person"`.
2. Victim (`principal V`) transfers 10 ICP to `swap_canister[subaccount(V)]` on the ICP ledger, intending to review the terms before committing.
3. Attacker reads `confirmation_text` via `get_init` (unauthenticated query).
4. Attacker submits ingress update:
   ```
   refresh_buyer_tokens({
     buyer: "<V's principal text>",
     confirmation_text: "I confirm I am not a US person"
   })
   ```
   from any identity (including anonymous, since the canister does not check).
5. `refresh_buyer_token_e8s` resolves `p = V`, validates the confirmation text (passes), reads `balance(swap[subaccount(V)]) = 10 ICP`, and writes `buyers[V] = {amount_icp_e8s: 10_0000_0000}`.
6. Victim's 10 ICP is now committed. `error_refund_icp` is unavailable. Victim never signed the confirmation text. [7](#0-6)

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

**File:** rs/sns/swap/canister/canister.rs (L231-235)
```rust
#[update]
async fn new_sale_ticket(request: NewSaleTicketRequest) -> NewSaleTicketResponse {
    log!(INFO, "new_sale_ticket");
    swap_mut().new_sale_ticket(&request, caller_principal_id(), time())
}
```

**File:** rs/sns/swap/canister/canister.rs (L252-256)
```rust
#[update]
fn notify_payment_failure(_request: NotifyPaymentFailureRequest) -> NotifyPaymentFailureResponse {
    log!(INFO, "notify_payment_failure");
    swap_mut().notify_payment_failure(&caller_principal_id())
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

**File:** rs/sns/swap/src/swap.rs (L1134-1163)
```rust
    pub async fn refresh_buyer_token_e8s(
        &mut self,
        buyer: PrincipalId,
        confirmation_text: Option<String>,
        this_canister: CanisterId,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> Result<RefreshBuyerTokensResponse, String> {
        use swap_participation::*;

        // These two checks need to be repeated after awaiting the response from the ICP ledger.
        self.validate_lifecycle_is_open()
            .map_err(context_before_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_before_awaiting_icp_ledger_response)?;

        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;

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

**File:** rs/sns/swap/src/swap.rs (L1200-1225)
```rust
        // Check that the minimum amount has been transferred before
        // actually creating an entry for the buyer.
        if e8s < params.min_participant_icp_e8s {
            return Err(format!(
                "Amount transferred: {}; minimum required to participate: {}",
                e8s, params.min_participant_icp_e8s
            ));
        }
        let max_participant_icp_e8s = params.max_participant_icp_e8s;

        let old_amount_icp_e8s = self
            .buyers
            .get(&buyer.to_string())
            .map_or(0, |buyer| buyer.amount_icp_e8s());

        if old_amount_icp_e8s >= e8s {
            // Already up-to-date. Strict inequality can happen if messages are re-ordered.
            return Ok(RefreshBuyerTokensResponse {
                icp_accepted_participation_e8s: old_amount_icp_e8s,
                icp_ledger_account_balance_e8s: e8s,
            });
        }
        // Subtraction safe because of the preceding if-statement.
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
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
