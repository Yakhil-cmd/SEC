### Title
Unprivileged Caller Can Force SNS Swap Participation on Behalf of Any Buyer, Bypassing Confirmation Text Requirement - (File: `rs/sns/swap/canister/canister.rs`)

---

### Summary
The `refresh_buyer_tokens` update method on the SNS Swap canister accepts an arbitrary `buyer` principal in its request payload and performs no check that the caller matches the specified buyer. Any unprivileged ingress sender can call this method with a victim's principal as `buyer` and supply the (publicly known) `confirmation_text`, forcing the victim's ICP — already deposited in the swap subaccount — into a committed swap participation without the victim's explicit consent.

---

### Finding Description

The `refresh_buyer_tokens` canister endpoint reads the `buyer` field from the request. If the field is non-empty, it is parsed directly as a `PrincipalId` with no check that `caller == buyer`:

```rust
// rs/sns/swap/canister/canister.rs
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← attacker-controlled
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, ...)
        .await
``` [1](#0-0) 

Inside `refresh_buyer_token_e8s`, the `confirmation_text` supplied by the caller is validated against the SNS-configured text:

```rust
// rs/sns/swap/src/swap.rs
self.validate_confirmation_text(confirmation_text)?;
``` [2](#0-1) 

The confirmation text is set during SNS initialization and is publicly readable from the canister state via `get_init`. Because it is public, any attacker can read it and supply it in a call targeting a victim's principal. The proto definition confirms the field is caller-supplied:

```proto
message RefreshBuyerTokensRequest {
  // If not specified, the caller is used.
  string buyer = 1;
  optional string confirmation_text = 2;
}
``` [3](#0-2) 

The function then reads the ICP balance from the victim's subaccount on the swap canister and, if sufficient, records the victim as a committed buyer:

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),
};
``` [4](#0-3) 

Once registered, the victim's ICP is locked into the swap. If the swap commits, the ICP is converted to SNS tokens and the victim cannot recover their ICP.

---

### Impact Explanation

A victim who has deposited ICP into the swap canister's subaccount (step 1 of the participation flow) but has not yet called `refresh_buyer_tokens` — perhaps because they are reconsidering participation, or because the SNS requires a confirmation text they have not yet agreed to — can have their participation forcibly committed by any attacker. The attacker:

1. Reads the public `confirmation_text` from the SNS init payload via `get_init`.
2. Calls `refresh_buyer_tokens` with `buyer = victim_principal` and `confirmation_text = <public_text>`.
3. The swap canister registers the victim as a buyer, locking their ICP.

If the swap subsequently commits, the victim's ICP is irreversibly converted to SNS tokens. The confirmation text mechanism — designed to ensure informed consent — is completely bypassed. This is a **governance authorization bug**: a user's financial position is altered without their authorization.

---

### Likelihood Explanation

- The attack requires only a standard ingress call to a public canister endpoint; no privileged access, no key material, no threshold corruption.
- The `confirmation_text` is publicly readable from canister state.
- The victim must have already deposited ICP into the swap subaccount, which is a normal intermediate state in the two-step participation flow.
- The window of vulnerability is the period between the victim's ICP deposit and their own call to `refresh_buyer_tokens`. This window can be minutes to hours.
- Motivation exists: an attacker who wants a swap to commit (e.g., a large SNS token holder) could force marginal participants in to reach the minimum ICP target.

---

### Recommendation

Add a caller-identity check inside `refresh_buyer_tokens`. The `buyer` field should only be accepted if it matches the caller, or if the caller is an explicitly authorized proxy (analogous to how `notify_create_canister` in the CMC enforces `authorize_caller_to_call_notify_create_canister_on_behalf_of_creator`):

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let buyer = PrincipalId::from_str(&arg.buyer).unwrap();
        // Enforce: caller must be the buyer
        if buyer != caller {
            panic!("Caller is not authorized to refresh tokens on behalf of another buyer");
        }
        buyer
    };
    ...
}
```

The analogous pattern already exists in the CMC: [5](#0-4) 

---

### Proof of Concept

**Setup:** SNS swap is in `Open` state with a `confirmation_text = "I agree to the terms"`. Victim (`VICTIM`) has transferred 10 ICP to the swap canister's subaccount for `VICTIM` on the ICP ledger, but has not yet called `refresh_buyer_tokens`.

**Attack:**

```bash
# Attacker reads the confirmation text from the SNS init
dfx canister call <swap_canister_id> get_init '(record {})'
# Returns: confirmation_text = opt "I agree to the terms"

# Attacker forces victim's participation
dfx canister call <swap_canister_id> refresh_buyer_tokens '(record {
    buyer = "VICTIM_PRINCIPAL_TEXT_ID";
    confirmation_text = opt "I agree to the terms"
})'
```

**Result:** The swap canister reads the ICP balance from `VICTIM`'s subaccount, finds 10 ICP, and registers `VICTIM` as a committed buyer. `VICTIM`'s ICP is now locked. If the swap commits, `VICTIM` receives SNS tokens instead of their ICP — without ever having called `refresh_buyer_tokens` themselves or having agreed to the confirmation text. [6](#0-5) [7](#0-6)

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

**File:** rs/nns/cmc/src/main.rs (L1438-1475)
```rust
fn authorize_caller_to_call_notify_create_canister_on_behalf_of_creator(
    caller: PrincipalId,
    creator: PrincipalId,
) -> Result<(), NotifyError> {
    if caller == creator {
        return Ok(());
    }

    // This is a hack to enable testing (related features) of nns-dapp. In
    // tests, the nns-dapp backend canister happens to use ID of the production
    // ICP ledger archive 1 canister. Ideally, the test nns-dapp backend
    // canister would have the same ID as the production nns-dapp backend
    // canister. This difference should probably be considered a bug. This hack
    // can be removed after that bug is fixed.
    const TEST_NNS_DAPP_BACKEND_CANISTER_ID: CanisterId = ICP_LEDGER_ARCHIVE_1_CANISTER_ID;
    lazy_static! {
        static ref ALLOWED_CALLERS: [PrincipalId; 2] = [
            PrincipalId::from(*NNS_DAPP_BACKEND_CANISTER_ID),
            PrincipalId::from(TEST_NNS_DAPP_BACKEND_CANISTER_ID),
        ];
    }

    if ALLOWED_CALLERS.contains(&caller) {
        return Ok(());
    }

    // Other is used, because adding a Unauthorized variant to NotifyError would
    // confuse old clients.
    let err = NotifyError::Other {
        error_code: NotifyErrorCode::Unauthorized as u64,
        error_message: format!(
            "{caller} is not authorized to call notify_create_canister on behalf \
             of {creator}. (Do not retry, because the same result will occur.)",
        ),
    };

    Err(err)
}
```
