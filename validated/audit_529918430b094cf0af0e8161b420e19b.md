Audit Report

## Title
Unauthenticated Third-Party Can Force SNS Swap Participation on Behalf of Any Principal, Bypassing Confirmation-Text Consent Gate - (File: rs/sns/swap/canister/canister.rs)

## Summary

The `refresh_buyer_tokens` update method accepts an arbitrary `buyer` principal from the request payload and uses it without verifying it matches `ic_cdk::caller()`. Any unprivileged caller can invoke this endpoint with a victim's principal as `buyer`, supplying the publicly-visible confirmation text, and force the victim's ICP (already sitting in their swap subaccount) into a committed participation record. Once registered, the ICP is locked in the swap until finalization, and `error_refund_icp` is blocked until the sweep completes—meaning a committed swap irreversibly converts the victim's ICP into SNS tokens they never explicitly agreed to purchase.

## Finding Description

**Root cause — no caller-equality check in the canister handler.**

In `rs/sns/swap/canister/canister.rs` L130–134, when `arg.buyer` is non-empty the handler resolves the target principal directly from the attacker-controlled string with no comparison against `caller_principal_id()`:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // ← attacker-controlled
};
```

The resolved `p` is passed straight into `swap_mut().refresh_buyer_token_e8s(p, arg.confirmation_text, ...)`.

**Confirmation text provides no real barrier.**

`validate_confirmation_text` (swap.rs L363–384) simply compares the caller-supplied string against `self.init_or_panic().confirmation_text`, which is a public field readable by anyone from the canister's state. There is no secret or per-user nonce involved.

**Ticket system is not a mandatory gate.**

The ticket check at swap.rs L1250–1272 is guarded by `if let Some(ticket) = ...`; the comment at L1271 explicitly states: *"If there exists no ticket for the buyer, the payment flow will simply ignore the ticket."* A victim who transferred ICP directly (without going through the ticket flow) has no open ticket, so this check is skipped entirely.

**`error_refund_icp` is blocked once participation is registered.**

`error_refund_icp` (swap.rs L1932–1936) requires the swap to be in `Aborted` or `Committed` state. After finalization, if the victim's buyer record exists and `transfer_success_timestamp_seconds == 0` (sweep not yet complete), the refund is blocked with a precondition error (L1950–1959). If the swap committed and sweep completes, the ICP has already been sent to SNS governance and the victim holds SNS tokens instead.

**Exploit flow:**
1. Victim transfers ICP to `subaccount(swap_canister, victim_principal)` on the ICP ledger.
2. Attacker reads the public `confirmation_text` from the swap canister's init state.
3. Attacker calls `refresh_buyer_tokens({ buyer: "victim_principal_text", confirmation_text: Some("<public text>") })` with their own principal as the IC caller.
4. The swap canister reads the victim's subaccount balance, validates the (public) confirmation text, and records the victim as a committed buyer.
5. Victim's ICP is now locked. `error_refund_icp` is blocked until sweep. If the swap commits and sweep completes, the victim receives SNS tokens they never agreed to purchase.

## Impact Explanation

**High — Significant SNS security impact with concrete user harm.**

A victim who transferred ICP to the swap subaccount but had not yet confirmed participation (e.g., was reviewing terms, or the SNS required a confirmation text they had not agreed to) can have their participation forcibly registered by any on-chain actor. In a committed swap this results in the victim receiving SNS tokens instead of ICP against their will, with no recourse during the open period. This matches: *"High ($2,000–$10,000): Significant SNS or infrastructure security impact with concrete user or protocol harm"* and *"Unauthorized access to … canister-controlled funds where exploitation requires meaningful per-target work or other constraints."*

## Likelihood Explanation

The attack requires no privileged access. The attacker only needs:
- The victim's principal (observable on-chain from the ICP ledger deposit).
- The confirmation text (publicly readable from the swap canister's init state).

No special tooling, no victim interaction, no social engineering. Any canister or user can execute this against any principal that has deposited ICP to a swap subaccount without yet calling `refresh_buyer_tokens`. The attack is repeatable across all open SNS swaps that use a confirmation text.

## Recommendation

Enforce that when `buyer` is non-empty it must equal `caller_principal_id()`. If relayer/third-party notification is a desired feature, it should require an explicit on-chain pre-authorization from the buyer (e.g., a signed intent or a separate `authorize_relayer` call), not rely on a public confirmation string. Concretely, in `rs/sns/swap/canister/canister.rs`:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let requested = PrincipalId::from_str(&arg.buyer).unwrap();
    if requested != caller_principal_id() {
        panic!("caller {} is not authorized to refresh tokens for {}",
               caller_principal_id(), requested);
    }
    requested
};
```

## Proof of Concept

**Deterministic unit test plan (safe, local only):**

1. Set up a `Swap` in `Open` state with a `confirmation_text` set to `"I agree"`.
2. Simulate Alice transferring 10 ICP to `subaccount(swap_canister, alice_principal)` on the mock ICP ledger.
3. As Mallory (a different principal), call `swap_mut().refresh_buyer_token_e8s(alice_principal, Some("I agree".to_string()), swap_canister_id, &mock_ledger)`.
4. Assert the call returns `Ok(...)` and `swap.buyers` now contains an entry for `alice_principal` with `amount_icp_e8s = 10 * E8`.
5. Assert that calling `error_refund_icp` for Alice while the swap is still `Open` returns a precondition error.
6. Commit the swap; assert Alice's ICP is swept to SNS governance and Alice holds SNS tokens — without Alice ever having called `refresh_buyer_tokens` herself.

This test requires no mainnet interaction and directly exercises the vulnerable code path in `rs/sns/swap/src/swap.rs` using the existing `mock_stub` / `MockLedger` infrastructure already present in `rs/sns/swap/tests/swap.rs`.