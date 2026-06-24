Audit Report

## Title
Unprivileged Caller Can Bypass `confirmation_text` Consent Gate and Force Victim's ICP Into SNS Swap — (`rs/sns/swap/canister/canister.rs`)

## Summary
The `refresh_buyer_tokens` update endpoint accepts an arbitrary `buyer` principal without verifying that the caller equals that principal. Because the swap's `confirmation_text` is stored in public `Init` state and readable by anyone via `get_init`, any unprivileged caller can supply the public text verbatim on behalf of any victim, committing the victim's already-transferred ICP to the swap, recording them as having consented to terms they never explicitly accepted, and destroying their open ticket.

## Finding Description
In `rs/sns/swap/canister/canister.rs` (L130–134), the effective buyer principal is resolved without any caller-equality check:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()  // no caller == buyer assertion
};
```

This `p` is passed directly to `refresh_buyer_token_e8s` (`rs/sns/swap/src/swap.rs`, L1134). Inside that function, the only caller-supplied gate is `validate_confirmation_text` (L1150), implemented at L363–384:

```rust
(Some(expected_text), Some(text)) => {
    if &text != expected_text { Err(...) } else { Ok(()) }
}
```

This check compares the supplied text against the value stored in `Init.confirmation_text` (proto field 15, `swap.proto` L325). That field is part of the public `Init` struct returned by the `get_init` query endpoint — it is not a secret. Any caller can read it and replay it verbatim.

After passing the text check, the function reads the ICP ledger balance of `principal_to_subaccount(&buyer)` (L1154–1163), registers it as the victim's participation (L1285–1288), and — if the victim had an open ticket — deletes it (L1270):

```rust
memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow_mut().remove(&principal));
```

No existing guard prevents a third party from supplying another user's principal. The proto comment ("If not specified, the caller is used") confirms the field is intentionally caller-overridable, but no compensating control was added for the `confirmation_text` case.

## Impact Explanation
This is a **High** severity finding matching: *"Unauthorized access to … ledgers, or canister-controlled funds where exploitation requires meaningful per-target work or other constraints."*

Three concrete harms result:

1. **Consent bypass**: `confirmation_text` is the SNS project's explicit legal/terms-of-service gate. A victim is permanently recorded on-chain as having agreed to terms they never read or accepted.
2. **Forced premature participation**: A victim who transferred ICP to their swap subaccount but had not yet decided to participate is locked into the swap. Their ICP cannot be recovered until the swap concludes (committed or aborted).
3. **Ticket destruction**: The victim's open ticket is silently deleted, permanently breaking the ticket-based payment flow for that user.

## Likelihood Explanation
The attack requires only: (a) the victim's principal (public on-chain), (b) the swap's `confirmation_text` (readable from the public `get_init` query), and (c) the victim having already transferred ICP to their swap subaccount (the normal first step of the payment flow). No privileged access, no key material, and no majority corruption is required. The attacker pays only the cycles cost of a single update call. The attack is repeatable against any victim who has staged a transfer but not yet called `refresh_buyer_tokens`.

## Recommendation
Enforce that the effective buyer equals the caller whenever a `confirmation_text` is required:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    if swap().init_or_panic().confirmation_text.is_some()
        && specified != caller_principal_id()
    {
        panic!("Caller must equal buyer when a confirmation_text is required");
    }
    specified
};
```

Alternatively, always use `caller_principal_id()` unconditionally and remove the ability to specify an arbitrary `buyer`, since any user can call the endpoint for themselves once they have transferred ICP.

## Proof of Concept
1. Deploy an SNS swap with `confirmation_text = "I confirm I have read the terms."`.
2. Victim transfers 10 ICP to `swap_canister[principal_to_subaccount(victim)]` on the ICP ledger (normal first step).
3. Victim has not yet called `refresh_buyer_tokens` — still deciding.
4. Attacker queries `get_init` on the swap canister to read the public `confirmation_text`.
5. Attacker sends an ingress update call to `refresh_buyer_tokens` with `buyer = victim_principal_text` and `confirmation_text = "I confirm I have read the terms."`.
6. The swap canister resolves `p = victim_principal`, passes `validate_confirmation_text` (the public text matches), reads the victim's 10 ICP balance from the ledger, commits it as the victim's participation, and deletes the victim's open ticket.
7. The victim is now a registered swap participant who has "agreed" to the terms, with their ICP locked — without ever having called the endpoint themselves.

A deterministic unit test can be written by adapting the existing `test_swap_participation_confirmation` test in `rs/sns/swap/tests/swap.rs` (L5639–5700): call `refresh_buyer_token_e8s` with `buyer = victim` but from a different caller principal, supply the correct public confirmation text, and assert that the victim's buyer state is registered and their ticket is removed.