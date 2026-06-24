Audit Report

## Title
Arbitrary Caller Can Commit Another User's ICP to SNS Swap Without Consent, Bypassing Confirmation Text - (File: rs/sns/swap/canister/canister.rs)

## Summary
The `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal from the request body with no enforcement that the caller equals the buyer. Because the `confirmation_text` consent check only compares the supplied string against the public SNS init payload — and that text is publicly known — any unprivileged caller can invoke `refresh_buyer_tokens` with `buyer = victim` and the correct public confirmation text, committing the victim's pre-deposited ICP to the swap without the victim ever explicitly consenting.

## Finding Description
In `rs/sns/swap/canister/canister.rs` (L128–143), the buyer principal is resolved from the request body rather than from the authenticated caller:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // no auth check
};
```

There is no guard requiring `p == caller_principal_id()` when `arg.buyer` is non-empty.

The resolved principal `p` is passed directly to `refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` (L1134). Inside that function, the only consent gate is `validate_confirmation_text` (L1149–1150), which compares the supplied string against `self.init_or_panic().confirmation_text` (L368–376). The confirmation text is part of the public SNS init payload, so every observer already knows it. There is no binding between the entity supplying the text and the buyer principal.

The code comment at L1123–1126 explicitly states: *"the **caller** of this function must accept this confirmation"* — but the implementation does not enforce this invariant when a non-empty `buyer` field is supplied.

After the confirmation text check passes, the function reads the ICP balance from the victim's subaccount (L1152–1163) and records it as committed participation (L1285–1291). The comment at L1130–1131 states that `error_refund_icp` is available only when `refresh_buyer_tokens` *fails*; a successful call by Eve locks Alice's ICP into the swap.

## Impact Explanation
This is a **High** severity finding matching: *"Significant SNS security impact with concrete user or protocol harm."* A victim who deposited ICP but chose not to proceed — because they disagreed with the swap terms after reading the confirmation text — can have their ICP forcibly committed by any unprivileged third party. If the swap commits, the victim receives SNS tokens they never agreed to purchase and cannot recover their ICP. The confirmation text mechanism, the sole user-facing consent gate for SNS swap participation, is rendered entirely ineffective.

## Likelihood Explanation
Medium-to-High. The precondition — victim has transferred ICP to their swap subaccount — is a routine step in the participation flow, so many users will be in this state during any open swap. The attacker requires only the victim's principal (publicly observable on-chain) and the confirmation text (public in the SNS init payload). No privileged access, key material, or special infrastructure is needed. Any unprivileged ingress sender can execute this against any number of victims.

## Recommendation
In `refresh_buyer_tokens`, enforce that when a non-empty `buyer` is supplied, the caller must equal the buyer principal:

```rust
if !arg.buyer.is_empty() && p != caller_principal_id() {
    panic!("Caller is not authorized to refresh tokens on behalf of another buyer.");
}
```

Long-term, remove the `buyer` field entirely and always derive the buyer from the authenticated caller. If third-party notification is required for automation, introduce an explicit allowlist of authorized notifier principals rather than permitting any caller to act on behalf of any buyer.

## Proof of Concept
1. Alice transfers 10 ICP to `swap_canister_subaccount(Alice)` on the ICP ledger during an open SNS swap that requires `confirmation_text = "I agree to the terms"`.
2. Alice reads the terms and decides not to participate. She does **not** call `refresh_buyer_tokens`.
3. Eve (any unprivileged principal) calls `refresh_buyer_tokens` with:
   ```
   RefreshBuyerTokensRequest {
       buyer: Alice.to_string(),
       confirmation_text: Some("I agree to the terms".to_string()),
   }
   ```
4. The canister resolves `p = Alice` (L130–134 of `canister.rs`), passes `validate_confirmation_text` (L1149–1150 of `swap.rs`), reads Alice's subaccount balance (L1152–1163), and records Alice as a committed buyer (L1285–1291).
5. Alice's ICP is now locked. `error_refund_icp` is unavailable (it applies only to failed calls). If the swap commits, Alice receives SNS tokens she never consented to purchase.

A deterministic integration test or PocketIC test can reproduce this by: (a) opening a swap with a `confirmation_text`, (b) having a test principal transfer ICP to the victim's subaccount, (c) calling `refresh_buyer_tokens` from a *different* principal with `buyer = victim` and the correct text, and (d) asserting the victim appears in `self.buyers` with a non-zero committed amount.