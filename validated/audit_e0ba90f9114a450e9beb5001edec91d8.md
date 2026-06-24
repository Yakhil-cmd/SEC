Audit Report

## Title
Unprivileged Caller Can Force Victim's ICP Into SNS Swap and Bypass Confirmation-Text Consent Gate — (`rs/sns/swap/canister/canister.rs`)

## Summary

The `refresh_buyer_tokens` update endpoint resolves the effective buyer from the caller-supplied `arg.buyer` field without verifying that the caller equals that principal. Because the swap's `confirmation_text` is publicly readable, any unprivileged attacker can supply both a victim's principal and the public confirmation text, committing the victim's pre-transferred ICP to the swap, satisfying the consent gate on their behalf, and destroying their open ticket — all without any action by the victim.

## Finding Description

In `rs/sns/swap/canister/canister.rs` at L130–134, the handler resolves the buyer principal as:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // no caller == buyer check
};
``` [1](#0-0) 

The resolved `p` is passed directly to `refresh_buyer_token_e8s` at L137 with the caller-supplied `arg.confirmation_text`. [2](#0-1) 

Inside `refresh_buyer_token_e8s` (`swap.rs` L1150), the only caller-supplied gate is:

```rust
self.validate_confirmation_text(confirmation_text)?;
``` [3](#0-2) 

This check validates that the supplied text matches the swap's stored `confirmation_text` — it does not verify that the caller is the buyer. Because `confirmation_text` is part of the public `Init` state (readable by anyone via `get_init`), it is not a secret. An attacker reads it and supplies it verbatim.

After passing that check, the function reads the ICP balance of the victim's subaccount:

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),  // victim's subaccount
};
``` [4](#0-3) 

It then registers that balance as the victim's participation and deletes the victim's open ticket:

```rust
memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow_mut().remove(&principal));
``` [5](#0-4) 

The proto definition confirms the field is intentionally caller-overridable ("If not specified, the caller is used"), but no authorization check was added to compensate. [6](#0-5) 

## Impact Explanation

This matches the **High** bounty impact: *Unauthorized access to governance assets / canister-controlled funds with concrete user harm.*

1. **Confirmation-text consent bypass.** SNS projects use `confirmation_text` as an explicit legal/compliance consent gate. Because any caller can supply the public text on behalf of any buyer, the gate is entirely ineffective — the victim is recorded as having consented to terms they never read or accepted.
2. **Forced premature participation.** A user who has transferred ICP to their swap subaccount but has not yet decided to participate is forcibly committed. Their ICP is locked until the swap concludes (committed or aborted), removing their ability to withdraw or reconsider.
3. **Ticket destruction.** The victim's open ticket is silently deleted, breaking the payment-flow state machine for that user and preventing future use of the ticket-based flow.

## Likelihood Explanation

The attack requires only: (1) the victim's principal (public on-chain), (2) the swap's `confirmation_text` (readable from the public `get_init` endpoint), and (3) the victim having already transferred ICP to their swap subaccount (the normal first step of the payment flow). No privileged access, key material, or majority corruption is needed. The attacker pays only the cycles cost of a single update call. The attack is repeatable against any victim who has pre-transferred ICP.

## Recommendation

Require that the effective buyer principal equals the caller whenever a `confirmation_text` is present, or unconditionally:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    if specified != caller_principal_id() {
        if swap().init_or_panic().confirmation_text.is_some() {
            panic!("Caller must be the buyer when a confirmation text is required");
        }
    }
    specified
};
```

Alternatively, remove the `buyer` override field entirely and always use `caller_principal_id()`, since any user can call the endpoint for themselves after transferring ICP.

## Proof of Concept

1. Deploy an SNS swap with `confirmation_text = "I confirm I have read the terms."`.
2. Victim transfers 10 ICP to `swap_canister[principal_to_subaccount(victim)]` on the ICP ledger (normal first step).
3. Victim has not yet called `refresh_buyer_tokens`.
4. Attacker calls `get_init` on the swap canister to read the public `confirmation_text`.
5. Attacker sends an ingress update call to `refresh_buyer_tokens` with `buyer = victim_principal_text` and `confirmation_text = "I confirm I have read the terms."`.
6. The swap canister resolves `p = victim_principal`, reads the victim's 10 ICP balance, validates the public confirmation text as if the victim supplied it, commits 10 ICP as the victim's participation, and deletes the victim's open ticket.
7. The victim is now a registered swap participant who has "agreed" to the terms, with their ICP locked — without ever having called the endpoint themselves.

A deterministic integration test using PocketIC can reproduce this by: creating a swap with `confirmation_text`, having a test identity transfer ICP, then calling `refresh_buyer_tokens` from a *different* identity with the victim's principal and the public confirmation text, and asserting the victim appears in `get_buyers_total` and their ticket is gone.

### Citations

**File:** rs/sns/swap/canister/canister.rs (L130-134)
```rust
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
```

**File:** rs/sns/swap/canister/canister.rs (L136-138)
```rust
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
```

**File:** rs/sns/swap/src/swap.rs (L1149-1150)
```rust
        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;
```

**File:** rs/sns/swap/src/swap.rs (L1154-1157)
```rust
            let account = Account {
                owner: this_canister.get().0,
                subaccount: Some(principal_to_subaccount(&buyer)),
            };
```

**File:** rs/sns/swap/src/swap.rs (L1270-1270)
```rust
            memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow_mut().remove(&principal));
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
