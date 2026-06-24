Audit Report

## Title
Missing Caller-Buyer Identity Check in `refresh_buyer_tokens` Enables Third-Party Confirmation-Text Bypass and Forced Swap Participation — (File: `rs/sns/swap/canister/canister.rs`)

## Summary

The `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal without verifying that the caller is that buyer. Because the `confirmation_text` consent gate is also caller-supplied and the text is publicly readable from swap state, any unprivileged ingress sender can commit another user's ICP into an open SNS swap — bypassing the explicit-consent mechanism the confirmation text is designed to enforce. Once committed, the victim's ICP cannot be recovered until the swap closes, and if the swap commits the ICP is swept to the SNS governance treasury in exchange for SNS tokens the victim never agreed to accept.

## Finding Description

In `rs/sns/swap/canister/canister.rs` lines 130–134, when `arg.buyer` is non-empty the function resolves the target principal directly from the caller-supplied string with no check that `caller == buyer`:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // no caller == buyer check
};
```

The resolved principal is passed directly to `refresh_buyer_token_e8s`, which calls `self.validate_confirmation_text(confirmation_text)` at `rs/sns/swap/src/swap.rs` line 1150. That function (lines 363–384) performs only a string equality check against the swap's publicly stored `init.confirmation_text`; it does not verify who is supplying the text. The confirmation text is set at SNS initialization and is readable by anyone from the swap canister's public state.

After the ledger balance query, if the victim's subaccount holds ICP above `min_participant_icp_e8s`, the function writes the victim's `BuyerState` at lines 1285–1288:

```rust
self.buyers
    .entry(buyer.to_string())
    .or_insert_with(|| BuyerState::new(0))
    .set_amount_icp_e8s(new_balance_e8s);
```

`error_refund_icp` (lines 1932–1935) is gated to `Lifecycle::Aborted || Lifecycle::Committed` only, so the victim has no recourse while the swap is OPEN. After the swap commits, `sweep_icp` transfers the victim's ICP to the SNS governance treasury; `error_refund_icp` then returns a precondition error ("ICP in escrow") until the sweep completes, after which the ICP is already gone.

The design intent of the `buyer` override field is confirmed by integration test helpers (e.g., `rs/sns/test_utils/src/state_test_helpers.rs` lines 291–300 and `rs/tests/nns/sns/lib/src/sns_deployment.rs` lines 919–926) that explicitly call `refresh_buyer_tokens` with a third-party caller and a specified `buyer`. The test at lines 807–818 shows the pre-transfer call fails only because the balance is zero (below minimum), not because of any authorization check — confirming no caller restriction exists.

## Impact Explanation

**High — Unauthorized access to canister-controlled funds with meaningful per-target constraints.**

A victim who has transferred ICP to their swap subaccount but has not yet called `refresh_buyer_tokens` (exercising the two-step opt-out window the confirmation text is designed to protect) can have their participation forcibly committed by any unprivileged third party. If the swap commits, the victim's ICP is permanently converted to SNS tokens they never consented to receive. The financial loss equals the victim's transferred ICP amount. The confirmation text mechanism — explicitly described in the proto as a consent gate ("a participant should send the confirmation text") — is rendered ineffective.

## Likelihood Explanation

The attack requires only a standard ingress update call. The attacker needs: (1) the victim's principal ID (observable on-chain from ledger transfer history), (2) the confirmation text (readable from the swap canister's public `init` state), and (3) the victim to have transferred ICP to their subaccount but not yet called `refresh_buyer_tokens`. The swap OPEN window is typically days to weeks. No privileged role, key material, or governance majority is required.

## Recommendation

When `arg.buyer` is non-empty, enforce that the caller equals the specified buyer before proceeding:

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

If third-party notification (e.g., by the NNS dapp on behalf of a user) is a required use case, the confirmation text check must be decoupled from the buyer-principal resolution: require the confirmation text to be absent when a third party calls on behalf of a buyer, and only accept it when `caller == buyer`. Alternatively, remove the `buyer` override field entirely and always use `caller_principal_id()`.

## Proof of Concept

1. Deploy an SNS swap with `confirmation_text = "I agree to the SNS terms"`.
2. Victim (`P_victim`) transfers 10 ICP to `swap_canister_subaccount(P_victim)` on the ICP ledger but does not call `refresh_buyer_tokens`.
3. Attacker (any principal) submits:
   ```
   refresh_buyer_tokens({
     buyer: "<P_victim_text>",
     confirmation_text: Some("I agree to the SNS terms")
   })
   ```
4. `validate_confirmation_text` passes (text matches). The ledger balance query returns 10 ICP. `BuyerState { amount_e8s: 10_ICP }` is written for `P_victim`.
5. Victim calls `error_refund_icp` while swap is OPEN → rejected ("Error refunds can only be performed when the swap is ABORTED or COMMITTED").
6. Swap commits. `sweep_icp` transfers `P_victim`'s 10 ICP to the SNS governance treasury. `P_victim` receives SNS tokens without having provided consent.

A deterministic unit test can be constructed by adapting `test_swap_participation_confirmation` in `rs/sns/swap/tests/swap.rs` (lines 5637–5700): call `refresh_buyer_token_e8s` with a `buyer` principal different from any simulated "caller" and a matching `confirmation_text`, and assert that `BuyerState` is written for the victim principal.