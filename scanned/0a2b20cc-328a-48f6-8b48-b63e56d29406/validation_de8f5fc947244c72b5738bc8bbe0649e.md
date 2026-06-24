### Title
Missing Caller Restriction in `refresh_buyer_tokens` Allows Third-Party Confirmation-Text Bypass and Forced Swap Participation — (File: `rs/sns/swap/canister/canister.rs`)

---

### Summary

The `refresh_buyer_tokens` update endpoint in the SNS Swap canister accepts an arbitrary `buyer` principal in its request without verifying that the caller is that buyer. Because the confirmation text (a consent gate) is also supplied by the caller rather than the buyer, any unprivileged ingress sender can register another user's ICP participation in an open SNS swap — bypassing the explicit-consent mechanism and locking the victim's ICP into the swap without their agreement.

---

### Finding Description

`refresh_buyer_tokens` in `rs/sns/swap/canister/canister.rs` resolves the target principal as follows:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // ← no check that caller == buyer
};
``` [1](#0-0) 

When `arg.buyer` is non-empty the function uses the attacker-supplied principal with no verification that the caller is that principal. The function then calls `refresh_buyer_token_e8s(p, arg.confirmation_text, …)`, passing the caller-supplied `confirmation_text` directly into the consent validation:

```rust
self.validate_confirmation_text(confirmation_text)?;
``` [2](#0-1) 

The SNS-specified confirmation text is stored in the swap's public state and is readable by anyone. The proto comment for `RefreshBuyerTokensRequest` explicitly notes the field is optional ("If not specified, the caller is used"), but provides no restriction when it *is* specified: [3](#0-2) 

The two-step participation flow (send ICP → confirm) is designed so a user can back out after sending ICP but before confirming. Once `refresh_buyer_tokens` is called for a victim, their `BuyerState` is written and their ICP is committed:

```rust
self.buyers
    .entry(buyer.to_string())
    .or_insert_with(|| BuyerState::new(0))
    .set_amount_icp_e8s(new_balance_e8s);
``` [4](#0-3) 

After this point the victim cannot use `error_refund_icp` to recover their ICP before the swap closes.

---

### Impact Explanation

**Governance authorization bug / forced participation.**

1. A victim sends ICP to their swap subaccount but has not yet called `refresh_buyer_tokens` (they may be reconsidering, or waiting).
2. An attacker calls `refresh_buyer_tokens` with `buyer = <victim_principal>` and the publicly known `confirmation_text`.
3. The victim's `BuyerState` is created/updated; their ICP is now committed.
4. If the swap commits, the victim's ICP is swept to the SNS governance treasury and the victim receives SNS tokens they never explicitly agreed to accept — losing the ICP they intended to reclaim.

The confirmation text is explicitly described as a consent gate ("a participant should send the confirmation text"), yet any third party can satisfy it on behalf of any buyer.

---

### Likelihood Explanation

- The attack requires only an ordinary ingress call — no privileged role, no key material, no governance majority.
- The confirmation text is set at SNS initialization and is readable from the swap canister's public state.
- The victim's principal ID and their ICP subaccount balance are observable on-chain.
- The swap lifecycle window (Open state) is typically days to weeks, giving ample time to execute.

---

### Recommendation

When `arg.buyer` is non-empty, enforce that the caller equals the specified buyer:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    if specified != caller_principal_id() {
        ic_cdk::trap("caller must match the specified buyer");
    }
    specified
};
```

Alternatively, remove the `buyer` override field entirely and always use `caller_principal_id()`, consistent with the pattern used by `notify_create_canister` in the CMC which explicitly rejects calls where `caller != creator`: [5](#0-4) 

---

### Proof of Concept

1. An SNS swap is deployed with `confirmation_text = "I agree to the SNS terms"`.
2. Victim (`P_victim`) transfers 10 ICP to `swap_canister_subaccount(P_victim)` on the ICP ledger.
3. Victim decides to wait / reconsider and does **not** call `refresh_buyer_tokens`.
4. Attacker submits an ingress update call to the swap canister:
   ```
   refresh_buyer_tokens({
     buyer: "<P_victim>",
     confirmation_text: "I agree to the SNS terms"
   })
   ```
5. `refresh_buyer_token_e8s` reads the ledger balance for `P_victim`'s subaccount (10 ICP), passes `validate_confirmation_text`, and writes `BuyerState { amount_e8s: 10_ICP }` for `P_victim`.
6. The swap commits. `sweep_icp` transfers `P_victim`'s 10 ICP to the SNS governance treasury. `P_victim` receives SNS tokens they never consented to accept and cannot recover their ICP. [6](#0-5) [1](#0-0)

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

**File:** rs/sns/swap/src/swap.rs (L1285-1291)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
        // We compute the current participation amounts once and store the result in Swap's state,
        // for efficiency reasons.
        self.update_total_participation_amounts();
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

**File:** rs/nns/cmc/src/main.rs (L1438-1474)
```rust
fn authorize_caller_to_call_notify_create_canister_on_behalf_of_creator(
    caller: PrincipalId,
    creator: PrincipalId,
) -> Result<(), NotifyError> {
    if caller == creator {
        return Ok(());
    }

    // This is a hack to enable testing (related features) of nns-dapp. In
    // tests, the nns-dapp backend canister happens to use ID of the production
    // ICP ledger archive 1 canister. Ideally, the test nns-dapp backend
    // canister would have the same ID as the production nns-dapp backend
    // canister. This difference should probably be considered a bug. This hack
    // can be removed after that bug is fixed.
    const TEST_NNS_DAPP_BACKEND_CANISTER_ID: CanisterId = ICP_LEDGER_ARCHIVE_1_CANISTER_ID;
    lazy_static! {
        static ref ALLOWED_CALLERS: [PrincipalId; 2] = [
            PrincipalId::from(*NNS_DAPP_BACKEND_CANISTER_ID),
            PrincipalId::from(TEST_NNS_DAPP_BACKEND_CANISTER_ID),
        ];
    }

    if ALLOWED_CALLERS.contains(&caller) {
        return Ok(());
    }

    // Other is used, because adding a Unauthorized variant to NotifyError would
    // confuse old clients.
    let err = NotifyError::Other {
        error_code: NotifyErrorCode::Unauthorized as u64,
        error_message: format!(
            "{caller} is not authorized to call notify_create_canister on behalf \
             of {creator}. (Do not retry, because the same result will occur.)",
        ),
    };

    Err(err)
```
