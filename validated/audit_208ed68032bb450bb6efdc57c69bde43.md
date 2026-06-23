### Title
Anyone Can Commit Another User's ICP to an SNS Swap by Bypassing the Caller Check in `refresh_buyer_tokens` - (File: rs/sns/swap/canister/canister.rs)

### Summary
The `refresh_buyer_tokens` endpoint in the SNS Swap canister accepts an arbitrary `buyer` principal in its request payload and uses it directly without verifying that the caller matches the specified buyer. This allows any unprivileged ingress sender to register another user's ICP participation in the swap — including accepting the swap's `confirmation_text` on their behalf — without the victim's knowledge or consent.

### Finding Description
In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update handler resolves the buyer principal as follows:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()
};
``` [1](#0-0) 

There is no check that `p == caller_principal_id()`. The resolved principal `p` is then passed directly to `refresh_buyer_token_e8s`, which:

1. Reads the ICP balance of `swap_canister_subaccount(p)` on the ICP ledger.
2. Validates the caller-supplied `confirmation_text` against the SNS-configured confirmation text.
3. Records `p`'s ICP as committed participation in the swap. [2](#0-1) 

The `confirmation_text` is set at SNS initialization time and is publicly readable from the swap state via `get_sale_parameters`. Because the text is public and the caller identity is never checked against the `buyer` field, any third party who knows the confirmation text can call `refresh_buyer_tokens` with `buyer = <victim_principal>` and commit the victim's already-deposited ICP to the swap.

The proto definition explicitly documents this open design:

```proto
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;
  optional string confirmation_text = 2;
}
``` [3](#0-2) 

### Impact Explanation
**Impact: Medium–High.**

A victim user who has transferred ICP to their swap subaccount (intending to review the confirmation text before committing) can have their participation forcibly registered by any attacker. If the swap succeeds, the victim receives SNS tokens instead of ICP. If the SNS token price at swap close is below the ICP price paid, the victim suffers a direct financial loss with no recourse, because the ICP has already been swept out of the subaccount by `finalize_swap`. The confirmation text mechanism — the only user-facing consent gate for swap participation — is rendered ineffective.

### Likelihood Explanation
**Likelihood: High.**

- The attack requires no privileged access, no key material, and no on-chain preconditions beyond the victim having deposited ICP into their swap subaccount.
- The `confirmation_text` is publicly visible in the swap state.
- The `buyer` field is a plain string in the Candid interface, settable by any ingress caller.
- The swap subaccount for any principal is deterministically derived via `principal_to_subaccount`, so the attacker can verify the victim's balance before attacking.

### Recommendation
Add a caller-identity check inside `refresh_buyer_tokens` (or inside `refresh_buyer_token_e8s`) that rejects calls where a non-empty `buyer` field does not match the ingress caller:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    let requested = PrincipalId::from_str(&arg.buyer).unwrap();
    if requested != caller_principal_id() {
        panic!("buyer field must match the caller");
    }
    requested
};
```

This mirrors the pattern used by every other asset-moving endpoint in the IC ecosystem (e.g., `icrc1_transfer` always derives `from` from `msg_caller()`). [4](#0-3) 

### Proof of Concept

**Setup:**
- SNS swap is `Open` with `confirmation_text = "I accept the terms"`.
- Victim (`V`) has transferred 100 ICP to `swap_canister_subaccount(V)` on the ICP ledger, intending to read the terms before confirming.

**Attack (attacker `A`, any non-anonymous principal):**

```
dfx canister call <swap_canister_id> refresh_buyer_tokens \
  '(record { buyer = "<V_principal_text>"; confirmation_text = opt "I accept the terms" })'
```

**Result:**
- `refresh_buyer_token_e8s` reads the 100 ICP balance for `V`'s subaccount.
- `validate_confirmation_text` passes because the attacker supplied the correct public text.
- `V`'s 100 ICP is recorded as committed participation.
- When `finalize_swap` runs, `V`'s ICP is swept to the SNS treasury and `V` receives SNS tokens — without `V` ever having called `refresh_buyer_tokens` themselves. [5](#0-4) [1](#0-0)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L128-143)
```rust
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

**File:** rs/ledger_suite/icrc1/ledger/src/main.rs (L676-679)
```rust
async fn icrc1_transfer(arg: TransferArg) -> Result<Nat, TransferError> {
    let from_account = Account {
        owner: ic_cdk::api::msg_caller(),
        subaccount: arg.from_subaccount,
```
