### Title
Unprivileged Caller Can Force SNS Swap Participation for Any Buyer, Bypassing Confirmation-Text Consent - (`rs/sns/swap/canister/canister.rs`)

### Summary
The `refresh_buyer_tokens` update endpoint in the SNS swap canister accepts a caller-supplied `buyer` principal and never verifies that the caller is that principal. Any unprivileged ingress sender can therefore trigger participation registration on behalf of any other user. When a SNS configures a `confirmation_text` as an explicit consent gate, this design allows an attacker to supply the (publicly visible) confirmation text on behalf of a victim, committing the victim's already-deposited ICP to the swap without the victim's explicit consent through this function.

### Finding Description

**Root cause — no caller-vs-buyer identity check**

In `rs/sns/swap/canister/canister.rs` the `refresh_buyer_tokens` handler resolves the buyer from the request argument, not from the authenticated caller:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // ← any caller, any buyer
};
``` [1](#0-0) 

There is no assertion that `caller_principal_id() == p`. The resolved principal is passed directly to `refresh_buyer_token_e8s`: [2](#0-1) 

**Confirmation-text consent mechanism is bypassed**

The SNS swap allows a project to require users to explicitly acknowledge a `confirmation_text` before their ICP is accepted. The validation only checks that the supplied string matches the expected string; it does not verify that the caller is the buyer:

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
            if &text != expected_text { Err(...) } else { Ok(()) }
        }
        ...
    }
}
``` [3](#0-2) 

Because the `confirmation_text` is set in the public SNS init payload and is readable by anyone, an attacker can supply it on behalf of a victim.

**Participation is committed against the victim's subaccount balance**

`refresh_buyer_token_e8s` reads the ICP balance of the buyer's subaccount on the swap canister and records it as accepted participation:

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),   // victim's subaccount
};
icp_ledger.account_balance(account).await ...
``` [4](#0-3) 

Once committed, the ICP is locked in the swap until finalization.

**Intentional third-party callability confirmed in integration tests**

The integration test suite explicitly demonstrates that a `default_sns_agent` (a different identity) can successfully call `refresh_buyer_tokens` for `wealthy_user_identity.principal_id` once that user has a balance in their subaccount: [5](#0-4) 

### Impact Explanation

A victim who transferred ICP to the swap subaccount — intending to review the confirmation text before deciding — can have their participation forcibly committed by any unprivileged attacker who:
1. Knows the victim's principal (public information).
2. Supplies the correct `confirmation_text` (also public).

The victim's ICP is locked in the swap until finalization. If the swap commits, the victim receives SNS tokens instead of ICP, which they may not have wanted. The `confirmation_text` consent mechanism — the only explicit per-user consent gate in the protocol — is rendered ineffective.

### Likelihood Explanation

The attack requires no privileged access, no leaked secrets, and no governance majority. The attacker needs only:
- The victim's principal (derivable from any on-chain interaction).
- The `confirmation_text` (readable from the public SNS init payload).
- The victim to have already sent ICP to the swap subaccount (a normal step in the participation flow, done before calling `refresh_buyer_tokens`).

Any user who deposits ICP and pauses before confirming is exposed for the entire window between their ICP transfer and their own call to `refresh_buyer_tokens`.

### Recommendation

Add a caller-identity check before accepting a non-empty `buyer` field:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let requested = PrincipalId::from_str(&arg.buyer).unwrap();
    if requested != caller_principal_id() {
        panic!("caller is not the specified buyer");
    }
    requested
};
```

Alternatively, remove the ability to specify a buyer other than the caller entirely, requiring each participant to call `refresh_buyer_tokens` themselves.

### Proof of Concept

1. SNS swap is `Open` with `confirmation_text = "I agree to the terms"` (public).
2. Victim transfers 5 ICP to `Account { owner: swap_canister_id, subaccount: principal_to_subaccount(victim) }` on the ICP ledger, intending to review the terms before confirming.
3. Attacker (any principal) sends an ingress update to the swap canister:
   ```
   refresh_buyer_tokens({
     buyer: victim_principal.to_string(),
     confirmation_text: Some("I agree to the terms")
   })
   ```
4. The swap canister resolves `p = victim_principal`, reads the 5 ICP balance from the victim's subaccount, validates the (attacker-supplied) confirmation text, and records the victim as a committed participant.
5. Victim's 5 ICP is now locked in the swap. The victim never explicitly accepted the confirmation text through their own call.

### Citations

**File:** rs/sns/swap/canister/canister.rs (L128-134)
```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
```

**File:** rs/sns/swap/canister/canister.rs (L136-142)
```rust
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    {
        Ok(r) => r,
        Err(msg) => panic!("{}", msg),
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

**File:** rs/tests/nns/sns/lib/src/sns_deployment.rs (L919-926)
```rust
    // Use the default identity to call refresh_buyer_tokens for the wealthy user
    let res_4 = {
        let request = sns_request_provider
            .refresh_buyer_tokens(Some(wealthy_user_identity.principal_id), None);
        block_on(default_sns_agent.call_and_parse(&request))
            .result()
            .unwrap()
    };
```
