### Title
Any Unprivileged Caller Can Force Victim's ICP Into SNS Swap, Bypassing Confirmation-Text Consent Gate — (`rs/sns/swap/canister/canister.rs`)

---

### Summary

The `refresh_buyer_tokens` update endpoint on the SNS Swap canister accepts an arbitrary `buyer` principal in its request payload and performs no check that the caller is that buyer. Any unprivileged ingress sender can therefore call the endpoint on behalf of any other user, committing that user's ICP to the swap and — critically — satisfying the swap's `confirmation_text` consent gate on their behalf, even though the victim never explicitly agreed.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` handler resolves the effective buyer principal as follows:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // ← no caller == buyer check
};
``` [1](#0-0) 

When `arg.buyer` is non-empty, the resolved principal `p` is passed directly to `refresh_buyer_token_e8s` without any verification that `caller_principal_id() == p`. The proto definition explicitly documents this as "If not specified, the caller is used", confirming the field is intentionally caller-overridable. [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the only caller-supplied gate is the confirmation text:

```rust
self.validate_confirmation_text(confirmation_text)?;
``` [3](#0-2) 

However, the confirmation text is stored in the swap's public `Init` state and is readable by anyone via `get_init`. Because the text is not a secret, any attacker can read it and supply it verbatim in their call. After passing that check, the function reads the ICP ledger balance of the victim's subaccount, registers it as the victim's participation, and deletes the victim's open ticket:

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),   // victim's subaccount
};
icp_ledger.account_balance(account).await ...
``` [4](#0-3) 

```rust
memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow_mut().remove(&principal));
``` [5](#0-4) 

The victim's ICP is then committed to the swap and their ticket is destroyed, all without any action by the victim.

---

### Impact Explanation

1. **Confirmation-text consent bypass.** SNS projects use `confirmation_text` as an explicit consent gate (e.g., a legal disclaimer). Because any caller can supply the public text on behalf of any buyer, the gate is entirely ineffective. A victim is recorded as having consented to terms they never read or accepted.

2. **Forced premature participation.** A user who has transferred ICP to their swap subaccount but has not yet decided to participate (e.g., waiting to observe swap progress) can be forced into the swap by an attacker. Their ICP is locked until the swap concludes (committed or aborted).

3. **Ticket destruction.** The victim's open ticket is silently deleted, breaking the payment-flow state machine for that user and preventing them from using the ticket-based flow in the future.

---

### Likelihood Explanation

The attack requires only:
- Knowledge of the victim's principal (public on-chain information).
- Knowledge of the swap's `confirmation_text` (readable from the public `get_init` endpoint).
- The victim having already transferred ICP to their swap subaccount (a normal first step in the payment flow).

No privileged access, no key material, and no majority corruption is required. The attacker pays only the cycles cost of an update call.

---

### Recommendation

Require that the effective buyer principal equals the caller when a `confirmation_text` is present, or unconditionally:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let specified = PrincipalId::from_str(&arg.buyer).unwrap();
    if specified != caller_principal_id() {
        // Only allow third-party refresh when no confirmation_text is required
        if swap().init_or_panic().confirmation_text.is_some() {
            panic!("Caller must be the buyer when a confirmation text is required");
        }
    }
    specified
};
```

Alternatively, remove the ability to specify an arbitrary `buyer` entirely and always use `caller_principal_id()`, relying on the fact that anyone can call the endpoint for themselves once they have transferred ICP.

---

### Proof of Concept

1. An SNS swap is opened with `confirmation_text = "I confirm I have read the terms."`.
2. Victim transfers 10 ICP to `swap_canister[principal_to_subaccount(victim)]` on the ICP ledger (normal first step of the payment flow).
3. Victim has not yet called `refresh_buyer_tokens` — they are still deciding.
4. Attacker reads the confirmation text via `get_init` on the swap canister.
5. Attacker sends an ingress update call to `refresh_buyer_tokens` with `buyer = victim_principal_text` and `confirmation_text = "I confirm I have read the terms."`.
6. The swap canister resolves `p = victim_principal`, reads the victim's 10 ICP balance, validates the (public) confirmation text as if the victim supplied it, commits 10 ICP as the victim's participation, and deletes the victim's open ticket.
7. The victim is now a registered swap participant who has "agreed" to the terms, with their ICP locked in the swap — without ever having called the endpoint themselves. [6](#0-5) [7](#0-6) [2](#0-1)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L127-143)
```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    {
        Ok(r) => r,
        Err(msg) => panic!("{}", msg),
    }
}
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

**File:** rs/sns/swap/src/swap.rs (L1134-1163)
```rust
    pub async fn refresh_buyer_token_e8s(
        &mut self,
        buyer: PrincipalId,
        confirmation_text: Option<String>,
        this_canister: CanisterId,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> Result<RefreshBuyerTokensResponse, String> {
        use swap_participation::*;

        // These two checks need to be repeated after awaiting the response from the ICP ledger.
        self.validate_lifecycle_is_open()
            .map_err(context_before_awaiting_icp_ledger_response)?;
        self.validate_possibility_of_direct_participation()
            .map_err(context_before_awaiting_icp_ledger_response)?;

        // User input validation doesn't expire after await, so this check doesn't need repetition.
        self.validate_confirmation_text(confirmation_text)?;

        // Look for the token balance of the specified principal's subaccount on 'this' canister.
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

**File:** rs/sns/swap/src/swap.rs (L1270-1270)
```rust
            memory::OPEN_TICKETS_MEMORY.with(|m| m.borrow_mut().remove(&principal));
```
