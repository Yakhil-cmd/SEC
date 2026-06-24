### Title
Caller-Controlled `buyer` Parameter in SNS Swap `refresh_buyer_tokens` Bypasses Confirmation-Text Consent Requirement - (File: `rs/sns/swap/canister/canister.rs`)

---

### Summary

The SNS Swap canister's `refresh_buyer_tokens` endpoint accepts a caller-supplied `buyer` principal string. When non-empty, this value is used verbatim as the buyer identity without verifying it matches the actual `msg_caller`. Any unprivileged ingress sender can therefore invoke `refresh_buyer_tokens` on behalf of any other principal, supplying the (publicly known) confirmation text and registering that principal's ICP participation without their explicit consent.

---

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update handler resolves the buyer identity as follows:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    PrincipalId::from_str(&arg.buyer).unwrap()   // ← no caller == buyer check
};
``` [1](#0-0) 

The resolved `p` is passed directly to `refresh_buyer_token_e8s`, which:

1. Derives the buyer's ICP subaccount from `p` and reads its ledger balance.
2. Validates the caller-supplied `confirmation_text` against the SNS-configured text.
3. Records the participation under `p` in `self.buyers`. [2](#0-1) 

The `confirmation_text` field is also part of the attacker-controlled request:

```proto
message RefreshBuyerTokensRequest {
  string buyer = 1;           // If not specified, the caller is used.
  optional string confirmation_text = 2;
}
``` [3](#0-2) 

The confirmation text is set at SNS initialization and is publicly readable from canister state. Because both `buyer` and `confirmation_text` are attacker-controlled parameters, the entire consent gate can be exercised by a third party on behalf of a victim who has already deposited ICP into the swap subaccount but has not yet chosen to participate.

The design intent of the `buyer` field is documented as a convenience ("if not specified, the caller is used"), but no authorization check enforces that a non-empty `buyer` must equal the caller. The existing system test explicitly demonstrates that a **different** principal can successfully call `refresh_buyer_tokens` for the wealthy user after the ICP transfer:

> "4. refresh_buyer_tokens (update) from the default user – should return res4  
>  5. refresh_buyer_tokens (update) from the wealthy user – should return res5; it should be that res5 == res4" [4](#0-3) 

---

### Impact Explanation

**Consent bypass / unauthorized state change.** The confirmation text is the only mechanism by which an SNS can require buyers to affirmatively agree to swap terms (e.g., risk disclosures, legal notices). Because any caller can supply both `buyer = <victim>` and the correct `confirmation_text`, a malicious actor can:

1. Wait for a victim to transfer ICP to the swap subaccount (a public ledger event).
2. Call `refresh_buyer_tokens { buyer: "<victim>", confirmation_text: "<known text>" }` from any identity.
3. The swap canister records the victim's participation as if the victim had consented.

If the swap commits, the victim's ICP is irreversibly converted to SNS tokens they never agreed to receive. If the swap aborts, the victim can reclaim ICP, but the confirmation-text consent invariant has been violated regardless. An attacker controlling many such calls can also push a swap past its ICP target, forcing early commitment.

**Severity: High** — direct unauthorized financial state change (ICP locked, SNS tokens issued) without victim consent; no privileged access required.

---

### Likelihood Explanation

**Medium-High.** The attacker needs only:
- An unprivileged ingress identity (any principal).
- Knowledge that a victim has sent ICP to the swap subaccount (observable on the public ICP ledger).
- The confirmation text (publicly readable from the swap canister's `get_init` query).

No keys, governance majority, or threshold assumptions are required. The attack is executable by any external user against any open SNS swap that uses a confirmation text.

---

### Recommendation

Add a caller-equals-buyer authorization check in the canister handler before passing the resolved principal to `refresh_buyer_token_e8s`:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let requested = PrincipalId::from_str(&arg.buyer).unwrap();
        // Enforce: only the buyer themselves may register their own participation.
        if requested != caller {
            panic!("Caller {} is not authorized to refresh tokens for buyer {}", caller, requested);
        }
        requested
    };
    // ...
}
```

This mirrors the pattern used throughout the IC codebase (e.g., `notify_create_canister` enforces `caller == creator` via `authorize_caller_to_call_notify_create_canister_on_behalf_of_creator`): [5](#0-4) 

---

### Proof of Concept

**Preconditions:**
- SNS swap is in `Open` lifecycle.
- SNS was initialized with `confirmation_text = "I agree to the terms"` (readable via `get_init` query).
- Victim (`principal V`) has transferred 10 ICP to the swap subaccount `principal_to_subaccount(V)` on the ICP ledger but has not yet called `refresh_buyer_tokens`.

**Attack steps (attacker principal `A`, any identity):**

```
# Step 1: Observe victim's ICP transfer on the public ledger (block explorer or ledger query).

# Step 2: Call refresh_buyer_tokens as attacker A, specifying victim V as buyer:
dfx canister call <swap_canister_id> refresh_buyer_tokens \
  '(record { buyer = "<principal V text>"; confirmation_text = opt "I agree to the terms" })'
  --identity attacker_A
```

**Result:** The swap canister records `V`'s participation (10 ICP) as confirmed, without `V` ever having called the endpoint or agreed to the confirmation text. If the swap subsequently commits, `V`'s ICP is converted to SNS tokens. [6](#0-5) [7](#0-6)

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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L841-851)
```text
//
// Only in lifecycle state `open`.
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;

  // To accept the swap participation confirmation, a participant should send
  // the confirmation text via refresh_buyer_tokens, matching the text set
  // during SNS initialization.
  optional string confirmation_text = 2;
}
```

**File:** rs/tests/nns/sns/lib/src/sns_deployment.rs (L749-757)
```rust
    //   1. refresh_buyer_tokens (update) from the default user - should return an error
    //   2. refresh_buyer_tokens (update) from the wealthy user - should return an error
    //   3. get_buyer_state (query) from the default user (should return "none" for the buyer state)
    // Afterwards, we will transfer some ICPs from this user's main account to their SNS sale subaccount.
    // Finally, we will check that the user's participate has been set up correctly after the transaction.
    // For this purpose, we submit three more calls:
    //   4. refresh_buyer_tokens (update) from the default user - should return res4
    //   5. refresh_buyer_tokens (update) from the wealthy user - should return res5; it should be that res5 == res4
    //   6. get_buyer_state (query) from the default user (should return "some" for the buyer state)
```

**File:** rs/nns/cmc/src/main.rs (L1438-1444)
```rust
fn authorize_caller_to_call_notify_create_canister_on_behalf_of_creator(
    caller: PrincipalId,
    creator: PrincipalId,
) -> Result<(), NotifyError> {
    if caller == creator {
        return Ok(());
    }
```
