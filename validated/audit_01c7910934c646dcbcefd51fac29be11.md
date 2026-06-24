Audit Report

## Title
Missing Caller Ownership Check in `refresh_buyer_tokens` Allows Unauthorized Swap Participation Registration — (File: `rs/sns/swap/canister/canister.rs`)

## Summary

The SNS swap canister's `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal without verifying that the caller is that buyer. Any unprivileged ingress sender can register another user's ICP participation in an SNS swap — including supplying the mandatory `confirmation_text` (terms-of-service consent gate) on the victim's behalf — as long as the victim has already transferred ICP to the swap canister's subaccount. If the swap commits, the victim's ICP is swept to the SNS treasury and SNS tokens are issued without the victim having explicitly consented.

## Finding Description

In `rs/sns/swap/canister/canister.rs` at lines 127–143, `refresh_buyer_tokens` resolves the buyer principal from the caller-supplied `arg.buyer` string field with no ownership check:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // no check that caller == p
};
```

The inner `refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` at lines 1134–1163 then:
1. Calls `validate_confirmation_text(confirmation_text)` — which only checks that the supplied text matches the SNS-configured expected text, not that the entity supplying it is the buyer.
2. Reads the ICP balance of the **buyer's** subaccount on the swap canister.
3. Writes the buyer's participation amount into `self.buyers` at lines 1285–1288.

There is no check anywhere in this path that `caller == buyer`. The `confirmation_text` validation at lines 363–384 is purely a string equality check against the publicly readable SNS init payload — it does not bind the consent to the caller's identity. The proto comment at line 844 confirms the field is optional and defaults to the caller only when omitted.

The attack path is fully reachable:
1. Victim transfers ICP to `swap_canister_subaccount(victim_principal)` on the ICP ledger.
2. Victim has not yet called `refresh_buyer_tokens` (reviewing terms, multi-step UI, or accidental transfer).
3. Attacker reads `confirmation_text` via the public `get_init` query endpoint.
4. Attacker calls `refresh_buyer_tokens({ buyer: "<victim_principal>", confirmation_text: opt "<correct_text>" })`.
5. Swap canister reads victim's ICP balance, validates the attacker-supplied consent text, and writes victim's participation into `self.buyers`.
6. If the swap commits, victim's ICP is swept to the SNS treasury and SNS tokens are issued — without the victim having agreed to the terms themselves.

Existing checks are insufficient: `validate_lifecycle_is_open`, `validate_possibility_of_direct_participation`, and `validate_confirmation_text` all pass because they do not verify caller identity against the buyer field.

## Impact Explanation

This is a **High** severity finding. An attacker can force a victim's ICP into an SNS swap without the victim's consent, bypassing the `confirmation_text` mechanism that exists precisely to record explicit user agreement to swap terms. Once the swap commits, the victim's ICP is irreversibly swept to the SNS treasury and SNS tokens are issued. The victim loses the ability to reclaim their ICP via `error_refund_icp` (which is only available after the swap closes) because they have been registered as a committed buyer. This constitutes unauthorized access to user funds and a concrete SNS governance/financial security impact with direct user harm, matching the allowed impact: *"Significant SNS security impact with concrete user or protocol harm"* and *"Unauthorized access to... canister-controlled funds where exploitation requires meaningful per-target work or other constraints."*

## Likelihood Explanation

- The attacker requires no privileged access: only a valid ingress identity.
- The victim's principal is public on-chain information.
- The victim's ICP subaccount balance on the swap canister is queryable via ledger queries.
- The `confirmation_text` is set at SNS initialization and is publicly readable from the swap canister's `get_init` query endpoint.
- The scenario where a user transfers ICP but has not yet called `refresh_buyer_tokens` is realistic: multi-step UI flows, accidental transfers, or deliberate delays pending review of terms are common.
- The attack is repeatable for any victim who has transferred ICP to the swap subaccount but not yet registered.

## Recommendation

Add a check in `refresh_buyer_tokens` that, when a non-empty `buyer` is specified, the caller must equal the buyer:

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

This ensures the `confirmation_text` consent gate is bound to the actual buyer's identity, not an arbitrary caller.

## Proof of Concept

A deterministic PocketIC or StateMachine integration test can prove this:

1. Set up a swap in `Open` state with a `confirmation_text` configured.
2. Mint ICP for `victim_principal` and transfer it to `swap_canister_subaccount(victim_principal)` on the ICP ledger.
3. Do **not** call `refresh_buyer_tokens` as the victim.
4. As `attacker_principal` (a different identity), call:
   ```
   execute_ingress_as(attacker_principal, swap_canister_id, "refresh_buyer_tokens",
       RefreshBuyerTokensRequest {
           buyer: victim_principal.to_string(),
           confirmation_text: Some("<correct_text>".to_string()),
       })
   ```
5. Assert the call succeeds and `swap.buyers` contains `victim_principal` with the correct ICP amount.
6. Finalize the swap; assert victim's ICP is swept and SNS tokens are issued to the victim — without the victim ever having called `refresh_buyer_tokens` themselves.