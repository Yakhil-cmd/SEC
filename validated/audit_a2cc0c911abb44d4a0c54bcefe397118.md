Audit Report

## Title
Missing Caller Ownership Check in `refresh_buyer_tokens` Bypasses SNS Swap Confirmation Text Consent Gate — (File: `rs/sns/swap/canister/canister.rs`)

## Summary

The SNS swap canister's `refresh_buyer_tokens` endpoint resolves the buyer principal from the caller-supplied `arg.buyer` field without verifying that the caller is that buyer. Any unprivileged ingress sender can register another user's ICP participation in an SNS swap — including supplying the mandatory `confirmation_text` (terms-of-service consent gate) on the victim's behalf — as long as the victim has already transferred ICP to the swap canister's subaccount. The `confirmation_text` mechanism, which exists to record explicit per-participant consent to swap terms, is entirely circumventable by a third party.

## Finding Description

In `rs/sns/swap/canister/canister.rs` at L130–134, the buyer principal is resolved from the caller-supplied string with no ownership check:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // no check that caller == p
};
```

This `p` is passed directly to `swap_mut().refresh_buyer_token_e8s(p, arg.confirmation_text, ...)`. Inside `refresh_buyer_token_e8s` (`rs/sns/swap/src/swap.rs` L1134–1163), the only validation of `confirmation_text` is `validate_confirmation_text` (L1150), which checks only that the supplied text matches the SNS-configured expected text — it does not verify that the entity supplying the text is the buyer whose funds are being committed. The function then reads the ICP balance of the **buyer's** subaccount (L1153–1163) and writes the buyer's participation into `self.buyers` (L1285–1291), all without any caller-identity check.

The proto comment for `RefreshBuyerTokensRequest.confirmation_text` states: *"a participant should send the confirmation text via refresh_buyer_tokens"* — but the code enforces no such constraint. The `confirmation_text` is set at SNS initialization and is publicly readable via the `get_init` query endpoint, so any attacker can supply the correct value.

A system integration test in `rs/tests/nns/sns/lib/src/sns_deployment.rs` (L743–757, L919–948) explicitly exercises and asserts that a third-party identity (the "default user") can successfully call `refresh_buyer_tokens` specifying the wealthy user's principal after the ICP transfer, and that the result equals the wealthy user calling for themselves. The pre-transfer calls fail only because the subaccount balance is zero — not due to any authorization check.

## Impact Explanation

This is a **High** severity finding matching: *"Significant SNS security impact with concrete user or protocol harm."*

A victim who transfers ICP to the swap canister's subaccount but has not yet called `refresh_buyer_tokens` (e.g., while reviewing the SNS terms, or due to a multi-step UI flow) can have their participation forcibly registered by any third party. The attacker supplies the correct `confirmation_text` — which is public — on the victim's behalf. If the swap subsequently commits, the victim's ICP is swept to the SNS treasury and the victim receives SNS tokens, a financial outcome they never explicitly consented to. The `confirmation_text` mechanism, which exists precisely to gate participation on explicit per-user consent to swap terms, is rendered meaningless: it becomes a shared password rather than a per-participant consent record.

## Likelihood Explanation

- No privileged access is required; any valid ingress identity suffices.
- The victim's principal is public on-chain information.
- The victim's ICP subaccount balance on the swap canister is queryable by anyone.
- The `confirmation_text` is set at SNS initialization and is publicly readable from the swap canister's `get_init` query endpoint.
- The window of vulnerability (ICP transferred but `refresh_buyer_tokens` not yet called) is realistic: multi-step UI flows, accidental transfers, or deliberate delays while reviewing terms all create this window.
- The attack is repeatable across any SNS swap that configures a `confirmation_text`.

## Recommendation

Add a caller-ownership check in `refresh_buyer_tokens` before passing the resolved principal to `refresh_buyer_token_e8s`. When a non-empty `buyer` is specified, assert that the caller equals the specified buyer:

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

Alternatively, enforce the same check inside `refresh_buyer_token_e8s` by threading the caller identity through and asserting `caller == buyer` before processing `confirmation_text`.

## Proof of Concept

1. Victim transfers 100 ICP to `swap_canister_subaccount(victim_principal)` on the ICP ledger but has not yet called `refresh_buyer_tokens`.
2. Attacker queries the swap canister's `get_init` endpoint to obtain the `confirmation_text`.
3. Attacker (any unprivileged principal) calls:
   ```
   refresh_buyer_tokens({
     buyer: "<victim_principal_id_string>",
     confirmation_text: opt "<correct_confirmation_text>"
   })
   ```
4. The swap canister resolves `p = victim_principal`, validates the attacker-supplied `confirmation_text` as matching (it does — it's public), reads the victim's 100 ICP subaccount balance, and writes the victim's participation into `self.buyers`.
5. If the swap commits, the victim's 100 ICP is swept to the SNS treasury and the victim receives SNS tokens — without ever having explicitly agreed to the swap terms themselves.

This is directly reproducible as a PocketIC integration test by adapting the existing test at `rs/tests/nns/sns/lib/src/sns_deployment.rs` L919–948, which already demonstrates a third-party identity successfully calling `refresh_buyer_tokens` for another user and having the call succeed.