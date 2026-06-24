Audit Report

## Title
Unprivileged Caller Can Bypass `confirmation_text` Consent Gate and Force Victim's ICP Into SNS Swap — (`rs/sns/swap/canister/canister.rs`)

## Summary

The `refresh_buyer_tokens` update endpoint accepts an arbitrary `buyer` principal with no check that the caller equals that principal. Because the swap's `confirmation_text` is stored in public `Init` state and readable by anyone, an attacker can supply it verbatim on behalf of any victim who has already transferred ICP to their swap subaccount, committing that ICP to the swap and recording the victim as having consented to terms they never accepted. The victim's open ticket is also silently destroyed.

## Finding Description

In `rs/sns/swap/canister/canister.rs` at lines 130–134, when `arg.buyer` is non-empty the resolved principal is taken directly from the request with no `caller == buyer` check:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // no caller == buyer assertion
};
``` [1](#0-0) 

This `p` is passed directly to `refresh_buyer_token_e8s`. Inside that function, `validate_confirmation_text` at `rs/sns/swap/src/swap.rs` lines 363–384 only performs a string equality check against the value stored in `Init`:

```rust
(Some(expected_text), Some(text)) => {
    if &text != expected_text { Err(...) } else { Ok(()) }
}
``` [2](#0-1) 

The `confirmation_text` is stored in the public `Init` struct (proto field 15) and is readable by anyone via `get_init`: [3](#0-2) 

After passing the text check, the function reads the ICP balance of the victim's subaccount and commits it as the victim's participation: [4](#0-3) 

The victim's open ticket is then deleted unconditionally if it exists: [5](#0-4) 

The proto comment `"If not specified, the caller is used"` confirms the `buyer` field is intentionally caller-overridable, but no guard was added to protect the `confirmation_text` consent gate from third-party callers. [6](#0-5) 

## Impact Explanation

This is a **High** severity finding. The `confirmation_text` mechanism is the only explicit consent gate for SNS swap participation — SNS projects use it for legal disclaimers or regulatory acknowledgements. Because any caller can read the text from `get_init` and supply it on behalf of any victim, the gate provides zero protection. Concretely: a victim who has transferred ICP to their swap subaccount but has not yet decided to participate can be forcibly registered as a participant who "agreed" to terms they never read, with their ICP locked until the swap concludes (committed or aborted). Their open ticket is also destroyed, breaking the ticket-based payment flow for that user. This matches the allowed impact: *"Significant SNS security impact with concrete user or protocol harm"* and *"Unauthorized access to governance assets/ledger-controlled funds."*

## Likelihood Explanation

The attack requires only: (1) the victim's principal, which is public on-chain; (2) the swap's `confirmation_text`, readable from the public `get_init` endpoint; and (3) the victim having already transferred ICP to their swap subaccount, which is the normal first step of the payment flow. No privileged access, key material, or majority corruption is needed. The attacker pays only the cycles cost of a single update call. The attack is repeatable against any victim in any open SNS swap that uses `confirmation_text`.

## Recommendation

Require that the effective buyer principal equals the caller whenever a `confirmation_text` is configured, or unconditionally:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    if specified != caller_principal_id() {
        if swap().init_or_panic().confirmation_text.is_some() {
            panic!("Caller must equal buyer when a confirmation_text is required");
        }
    }
    specified
};
```

Alternatively, remove the `buyer` override entirely and always use `caller_principal_id()`, since any user can call the endpoint for themselves after transferring ICP.

## Proof of Concept

1. Deploy an SNS swap with `confirmation_text = "I confirm I have read the terms."`.
2. Victim transfers 10 ICP to `swap_canister[principal_to_subaccount(victim)]` on the ICP ledger (normal first step).
3. Victim has not yet called `refresh_buyer_tokens` — still deciding.
4. Attacker calls `get_init` on the swap canister and reads `confirmation_text`.
5. Attacker sends an ingress update to `refresh_buyer_tokens` with `buyer = victim_principal_text` and `confirmation_text = "I confirm I have read the terms."`.
6. The swap canister resolves `p = victim_principal`, passes `validate_confirmation_text` (public text matches), reads the victim's 10 ICP balance from their subaccount, commits it as the victim's participation, and deletes the victim's open ticket.
7. The victim is now a registered swap participant recorded as having consented to the terms, with ICP locked — without ever having called the endpoint themselves.

A deterministic unit test can be written by adapting the existing `test_swap_participation_confirmation` test in `rs/sns/swap/tests/swap.rs` to call `refresh_buyer_token_e8s` with a `buyer` principal that differs from the simulated caller, supplying the correct confirmation text, and asserting that participation is registered and the ticket is removed. [7](#0-6)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L130-134)
```rust
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
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

**File:** rs/sns/swap/src/swap.rs (L1268-1271)
```rust
            // The requested balance in the ticket matches the balance to be topped up in the swap
            // --> Delete fully executed ticket, if it exists and proceed with the top up
            memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow_mut().remove(&principal));
            // If there exists no ticket for the buyer, the payment flow will simply ignore the ticket
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L322-325)
```text
  // An optional text that swap participants should confirm before they may
  // participate in the swap. If the field is set, its value should be plain
  // text with at least 1 and at most 1,000 characters.
  optional string confirmation_text = 15;
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

**File:** rs/sns/swap/tests/swap.rs (L5637-5700)
```rust
/// Test that the `refresh_buyer_token_e8s` function handles confirmations correctly.
#[test]
fn test_swap_participation_confirmation() {
    let confirmation_text = "Please confirm that 2+2=4".to_string();
    let another_text = "Please confirm that 2+2=5".to_string();
    let user = PrincipalId::new_user_test_id(1);
    let amount = 101 * E8;

    let buy_token = |swap: &mut Swap, confirmation_text: Option<String>| {
        swap.refresh_buyer_token_e8s(
            user,
            confirmation_text,
            SWAP_CANISTER_ID,
            &mock_stub(vec![LedgerExpect::AccountBalance(
                Account {
                    owner: SWAP_CANISTER_ID.get().into(),
                    subaccount: Some(principal_to_subaccount(&user)),
                },
                Ok(Tokens::from_e8s(amount)),
            )]),
        )
        .now_or_never()
        .unwrap()
    };

    // A. SNS specifies confirmation text & client sends confirmation text
    {
        let mut swap = SwapBuilder::new()
            .with_lifecycle(Open)
            .with_confirmation_text(confirmation_text.clone())
            .build();
        // A.1. The texts match
        assert_is_ok!(buy_token(&mut swap, Some(confirmation_text.clone())));
        // A.2. The texts do not match
        assert_is_err!(buy_token(&mut swap, Some(another_text)));
    }

    // B. SNS specifies confirmation text & client does not send a confirmation text
    {
        let mut swap = SwapBuilder::new()
            .with_lifecycle(Open)
            .with_confirmation_text(confirmation_text.clone())
            .build();
        assert_is_err!(buy_token(&mut swap, None));
    }

    // C. SNS does not specify confirmation text & client sends a confirmation text
    {
        let mut swap = SwapBuilder::new()
            .with_lifecycle(Open)
            .without_confirmation_text()
            .build();
        assert_is_err!(buy_token(&mut swap, Some(confirmation_text)));
    }

    // D. SNS does not specify confirmation text & client does not send a confirmation text
    {
        let mut swap = SwapBuilder::new()
            .with_lifecycle(Open)
            .without_confirmation_text()
            .build();
        assert_is_ok!(buy_token(&mut swap, None));
    }
}
```
