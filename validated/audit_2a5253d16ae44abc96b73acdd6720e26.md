Audit Report

## Title
Third-Party Caller Can Bypass Confirmation-Text Consent in SNS Swap `refresh_buyer_tokens` - (File: rs/sns/swap/canister/canister.rs)

## Summary
The `refresh_buyer_tokens` endpoint in the SNS Swap canister accepts an arbitrary `buyer` principal in its request payload and uses it directly without verifying that the caller matches the specified buyer. Because the SNS-configured `confirmation_text` is a public on-chain string, any unprivileged third party can read it and call `refresh_buyer_tokens(buyer=<victim>, confirmation_text=<text>)`, registering another principal's participation without that principal ever having explicitly agreed to the SNS terms. This bypasses the only explicit consent gate in the SNS swap participation flow.

## Finding Description
In `rs/sns/swap/canister/canister.rs` at lines 130–134, the effective buyer principal is resolved as:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // caller identity ignored
};
```

When `arg.buyer` is non-empty, the caller's identity is completely discarded. The resolved principal `p` is passed directly to `refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs`.

Inside `refresh_buyer_token_e8s`, the only consent check is `validate_confirmation_text` (lines 363–384 of `swap.rs`), which performs a simple string equality comparison between the caller-supplied text and the SNS-configured text stored in `self.init_or_panic().confirmation_text`. The SNS `confirmation_text` is a public, on-chain value readable by anyone via `get_state`. There is no check that the entity supplying the confirmation text is the same principal whose subaccount balance is being registered.

The exploit flow:
1. Alice transfers ICP to `swap_canister_subaccount(Alice)` — a public ledger event.
2. Eve reads the public `confirmation_text` from the swap canister state.
3. Eve calls `refresh_buyer_tokens({ buyer: "<Alice>", confirmation_text: Some("<text>") })` as any principal.
4. The swap canister queries Alice's subaccount balance, passes the confirmation text check, and registers Alice as a participant — without Alice ever having called `refresh_buyer_tokens`.

The `buyer` field is documented as "If not specified, the caller is used," and integration tests confirm the field was intentionally designed to allow third-party notification (e.g., `rs/tests/nns/sns/lib/src/sns_deployment.rs` lines 807–812 show a "default identity" calling on behalf of a "wealthy user"). However, this design was not reconciled with the later addition of the `confirmation_text` consent gate, which is explicitly described as requiring the *participant* to accept the terms.

## Impact Explanation
The `confirmation_text` is the sole mechanism by which a swap participant explicitly agrees to SNS-specific terms (legal disclaimers, tokenomics disclosures, etc.). A third party can satisfy this check on any victim's behalf, registering the victim's participation without their explicit agreement. Once registered, the victim's ICP is held in escrow and cannot be reclaimed until the swap closes (`error_refund_icp` is only available post-close in the ABORTED or COMMITTED lifecycle state). If the swap commits, the victim's ICP is swept to the SNS governance canister and the victim receives SNS tokens they never consented to receive. This constitutes a significant SNS security impact with concrete user harm — unauthorized commitment of user funds and bypass of the documented consent mechanism — matching the High impact class: "Significant SNS or infrastructure security impact with concrete user or protocol harm."

## Likelihood Explanation
All ICP ledger transfers are public; an attacker can trivially monitor the ledger for transfers to swap subaccounts. The `confirmation_text` is readable by anyone via `get_state`. No special privilege, key, or majority is required — a single unprivileged ingress call suffices. The attack is most impactful for SNS launches that set a non-empty `confirmation_text`, which is a documented and supported feature. The attack is repeatable for every principal who has deposited ICP without yet calling `refresh_buyer_tokens`.

## Recommendation
Validate that the caller matches the specified buyer when `arg.buyer` is non-empty:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    if specified != caller_principal_id() {
        panic!("caller must match the specified buyer");
    }
    specified
};
```

Alternatively, remove the `buyer` override field entirely and always use `caller_principal_id()`, since the ICP subaccount is already derived from the buyer's principal and there is no legitimate use case for a third party to register participation (including confirmation-text acceptance) on behalf of another principal.

## Proof of Concept
A deterministic integration test using PocketIC or StateMachine:

1. Initialize an SNS swap with `confirmation_text = "I agree to the terms"`.
2. Alice (principal A) transfers 10 ICP to `swap_canister_subaccount(A)` on the ICP ledger.
3. Eve (principal E, any other principal) calls:
   ```
   refresh_buyer_tokens({
     buyer: "<A's principal string>",
     confirmation_text: Some("I agree to the terms")
   })
   ```
   as principal E.
4. Assert: the swap canister's `buyers` map now contains an entry for A with `amount_icp_e8s > 0`.
5. Assert: A never called `refresh_buyer_tokens` herself.
6. Assert: `error_refund_icp` for A returns a precondition error ("ICP in escrow"), confirming A's funds are locked.

This test can be written directly against the existing `StateMachine`-based test infrastructure in `rs/sns/swap/tests/` using the `refresh_buyer_tokens` helper in `rs/sns/test_utils/src/state_test_helpers.rs`, modified to send the call as a different principal than the `buyer` field specifies.