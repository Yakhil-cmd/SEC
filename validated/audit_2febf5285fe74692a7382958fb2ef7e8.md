### Title
Unpermissioned `refresh_buyer_tokens` Allows Anyone to Force SNS Swap Participation for Another Buyer — (File: rs/sns/swap/canister/canister.rs)

### Summary
The SNS Swap canister's `refresh_buyer_tokens` endpoint accepts a `buyer` field that can be any principal, with no check that the caller equals the buyer. Any unprivileged ingress sender can call `refresh_buyer_tokens` specifying a victim's principal as `buyer`, forcing the victim's ICP (already sitting in the swap subaccount) to be registered as committed swap participation. This locks the victim's ICP until the swap closes and forces them into a financial commitment they did not intend to make, directly analogous to M-14's unpermissioned claim forcing users to realize losses.

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update handler resolves the buyer from the request argument with no caller-equality check:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // no check: caller == buyer
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

The `RefreshBuyerTokensRequest` proto explicitly documents that the `buyer` field defaults to the caller only when empty, meaning any non-empty principal string is accepted without authorization: [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the function reads the ICP balance from the victim's swap subaccount and registers it as committed participation:

```rust
let e8s = {
    let account = Account {
        owner: this_canister.get().0,
        subaccount: Some(principal_to_subaccount(&buyer)),
    };
    icp_ledger.account_balance(account).await ...
};
``` [3](#0-2) 

Once registered, the participation is committed and the ICP cannot be reclaimed until the swap closes (committed or aborted). The `confirmation_text` field is not a real access control: it is public information stored in the SNS init payload and readable by anyone via `get_sale_parameters`. Any attacker can read it and supply it verbatim. [4](#0-3) 

The `validate_confirmation_text` check compares the caller-supplied text against the SNS-configured text — it is a UX consent mechanism, not a secret: [5](#0-4) 

When the swap is near its `max_direct_participation_e8s` target, the accepted amount is capped:

```rust
let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
``` [6](#0-5) 

This means a victim who transferred 100 ICP may have only 50 ICP accepted (the remainder locked in the subaccount until swap close), receiving fewer SNS tokens than they intended — a direct pro-rata analog to M-14.

### Impact Explanation

A malicious unprivileged principal can:

1. Force any user who has transferred ICP to the swap subaccount into committed participation, even if the user was reconsidering or intended to reclaim their ICP.
2. Lock the victim's ICP until the swap closes (which can be days).
3. If the swap commits at an unfavorable clearing price (oversubscribed swap), the victim receives SNS tokens they did not want at a rate they found unfavorable — they cannot undo the commitment.
4. If the swap is near its ICP ceiling, the victim's participation is capped and the excess ICP is locked, causing the victim to receive fewer SNS tokens than their full ICP balance would have entitled them to.

This is a governance authorization bug with direct financial impact: forced irreversible financial commitment without the account owner's consent.

### Likelihood Explanation

The attack requires zero privileged access. The attacker needs only:
- The victim's principal ID (public, derivable from any on-chain interaction).
- The swap canister ID (public).
- The `confirmation_text` if set (public, readable via `get_sale_parameters` query).

Many SNS swaps do not set a `confirmation_text`, making the attack even simpler. The attack is most impactful during the OPEN lifecycle phase when the swap is near its ICP ceiling, a predictable and observable on-chain condition.

### Recommendation

Add a caller-equality check in the canister handler before dispatching to `refresh_buyer_token_e8s`:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let buyer = PrincipalId::from_str(&arg.buyer).unwrap();
        if buyer != caller {
            panic!("caller is not authorized to refresh tokens for another buyer");
        }
        buyer
    };
    ...
}
```

If third-party notification (e.g., by a protocol bot) is desired for UX, restrict it to a whitelist of trusted canister callers (not arbitrary ingress senders), mirroring the pattern used in `authorize_caller_to_call_notify_create_canister_on_behalf_of_creator` in the CMC. [7](#0-6) 

### Proof of Concept

1. Victim transfers 100 ICP to `swap_canister_subaccount(victim_principal)` on the ICP ledger.
2. Victim decides to reclaim their ICP (swap terms are unfavorable; swap is near its ceiling with only 50 ICP of capacity remaining).
3. Attacker queries `get_sale_parameters` to read the public `confirmation_text`.
4. Attacker submits ingress update call to `refresh_buyer_tokens` with `buyer = victim_principal.to_text()` and `confirmation_text = <public text>`.
5. The swap canister reads the victim's subaccount balance (100 ICP), caps accepted participation at 50 ICP (remaining capacity), and registers 50 ICP as the victim's committed participation.
6. Victim's 100 ICP is now locked in the swap subaccount until close; only 50 ICP will be accepted as participation; the other 50 ICP requires `error_refund_icp` after close.
7. If the swap commits, victim receives SNS tokens for only 50 ICP at the clearing price — fewer tokens than their full 100 ICP would have entitled them to, and at a rate they did not consent to.

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

**File:** rs/sns/swap/src/swap.rs (L1149-1151)
```rust
        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;

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

**File:** rs/sns/swap/src/swap.rs (L1223-1225)
```rust
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
```

**File:** rs/nns/cmc/src/main.rs (L1438-1475)
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
}
```
