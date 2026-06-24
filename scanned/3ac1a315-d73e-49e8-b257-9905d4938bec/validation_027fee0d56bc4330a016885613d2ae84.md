### Title
Unprivileged Caller Can Force Another Principal Into SNS Swap Participation, Bypassing Confirmation Text Consent — (`rs/sns/swap/canister/canister.rs`)

---

### Summary

The SNS Swap canister's `refresh_buyer_tokens` endpoint accepts a caller-supplied `buyer` principal with no check that `buyer == caller`. Any unprivileged ingress sender can trigger swap participation on behalf of any other principal who has pre-funded their swap subaccount, including supplying the publicly readable `confirmation_text` on the victim's behalf — recording their participation as if they explicitly agreed to the swap terms.

---

### Finding Description

`refresh_buyer_tokens` in `rs/sns/swap/canister/canister.rs` resolves the effective buyer principal from the request argument rather than from the authenticated caller:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // ← no caller == buyer check
};
``` [1](#0-0) 

This `p` is passed directly into `refresh_buyer_token_e8s`, which also receives the caller-supplied `confirmation_text`. The inner function has no caller parameter and performs no authorization check that the entity accepting the confirmation text is the same principal whose funds are being committed:

```rust
pub async fn refresh_buyer_token_e8s(
    &mut self,
    buyer: PrincipalId,
    confirmation_text: Option<String>,
    ...
) -> Result<RefreshBuyerTokensResponse, String> {
    ...
    self.validate_confirmation_text(confirmation_text)?;  // ← checked against SNS init, not caller identity
    let account = Account {
        owner: this_canister.get().0,
        subaccount: Some(principal_to_subaccount(&buyer)),  // ← victim's subaccount
    };
``` [2](#0-1) 

`validate_confirmation_text` only compares the supplied string against the SNS-configured text; it does not verify that the caller is the same principal as `buyer`: [3](#0-2) 

The `confirmation_text` is stored in the public `Init` struct and is readable by anyone via `get_init`. The `RefreshBuyerTokensRequest` proto explicitly documents the `buyer` field as "if not specified, the caller is used," implying third-party specification is an intended design — but no consent mechanism exists for the named buyer: [4](#0-3) 

---

### Impact Explanation

An attacker who knows a victim's principal has pre-funded their swap subaccount (a publicly observable ledger event) can:

1. Read the SNS swap's `confirmation_text` via the public `get_init` query.
2. Call `refresh_buyer_tokens` with `buyer = victim_principal` and the correct `confirmation_text`.
3. The swap canister records a `BuyerState` for the victim, locking their ICP into the swap as if they explicitly agreed to the terms.

Once the swap reaches the `COMMITTED` lifecycle, the victim's ICP is swept out and SNS tokens are distributed to them — this is irreversible. The victim's ICP is consumed without their explicit consent, and the `confirmation_text` mechanism (designed to enforce legal/terms agreement) is rendered meaningless. This is a direct analog to the SPTV2 bug: a caller specifies both a recipient and a participation level (the full subaccount balance, capped at `max_participant_icp_e8s`) for another user without that user's authorization. [5](#0-4) 

---

### Likelihood Explanation

- **No privileged access required**: any ingress sender can call `refresh_buyer_tokens`.
- **Victim precondition is observable**: ICP ledger transfers to the swap subaccount are public; an attacker can monitor for pre-funded subaccounts.
- **Confirmation text is public**: it is stored in the SNS init payload and returned by `get_init`.
- **Realistic scenario**: users commonly pre-fund their swap subaccounts before calling `refresh_buyer_tokens` themselves, creating a window for exploitation.

Likelihood: **Medium-High**.

---

### Recommendation

Add a caller-identity check in `refresh_buyer_tokens` before accepting a third-party `buyer` value. The fix should mirror the SPTV2 recommendation: if the `buyer` differs from the caller, either reject the call outright, or require a cryptographic signature from the `buyer` principal authorizing the participation. The minimal fix:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    // Only allow a third-party caller if buyer == caller
    if specified != caller_principal_id() {
        panic!("caller is not authorized to refresh tokens on behalf of {}", specified);
    }
    specified
};
``` [1](#0-0) 

---

### Proof of Concept

**Setup**: SNS swap is `Open`. Victim (`V`) transfers 10 ICP to `subaccount(swap_canister, V)` on the ICP ledger, intending to call `refresh_buyer_tokens` themselves later. The SNS `confirmation_text` is `"I agree to the terms"` (readable via `get_init`).

**Attack**:
```
Attacker → swap_canister.refresh_buyer_tokens({
    buyer: V.to_string(),
    confirmation_text: Some("I agree to the terms")
})
```

**Result**:
- `p` is set to `V` (attacker-controlled, no authorization check).
- `validate_confirmation_text` passes (attacker supplied the correct public string).
- `account_balance(subaccount(swap_canister, V))` returns 10 ICP.
- `BuyerState` for `V` is created with `amount_icp_e8s = 10 ICP`.
- `V` is now a committed swap participant. When the swap finalizes, `V`'s ICP is swept and SNS tokens are minted to `V` — without `V` ever calling `refresh_buyer_tokens` themselves or explicitly agreeing to the terms. [6](#0-5) [7](#0-6)

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
