### Title
Unauthenticated `buyer` Field in `refresh_buyer_tokens` Allows Forced Swap Participation and Confirmation-Text Consent Bypass - (File: `rs/sns/swap/canister/canister.rs`)

### Summary

The SNS Swap canister's `refresh_buyer_tokens` endpoint accepts an arbitrary `buyer` principal in its request without verifying that the caller is that buyer. This mirrors the original report's two-step, non-atomic token flow: a user first sends ICP to the swap canister's subaccount (step 1), then calls `refresh_buyer_tokens` to register participation (step 2). Because step 2 is not caller-gated, any unprivileged ingress sender can trigger it on behalf of any victim who has already completed step 1, forcing participation and bypassing the SNS-configured confirmation-text consent mechanism.

### Finding Description

The SNS Swap participation flow is a two-step process:

1. The user sends ICP to the swap canister's subaccount derived from their principal (`swap_canister[subaccount = principal_to_subaccount(buyer)]`).
2. The user calls `refresh_buyer_tokens` to notify the swap canister of the transfer and register their participation.

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` handler resolves the buyer principal from the request field without any caller-identity check:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // ← no check that caller == p
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

The `RefreshBuyerTokensRequest` type exposes `buyer` as a plain string field, publicly documented as "If not specified, the caller is used": [2](#0-1) 

Inside `refresh_buyer_token_e8s`, the swap checks the ICP balance at the subaccount derived from the supplied `buyer` principal and credits that buyer's participation record:

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),
};
icp_ledger.account_balance(account).await ...
``` [3](#0-2) 

The `confirmation_text` field — the SNS project's explicit consent mechanism — is also taken directly from the attacker-controlled request and validated against the SNS-configured text: [4](#0-3) 

Because the confirmation text is publicly readable (set at SNS initialization), any attacker can supply the correct text on behalf of any victim.

### Impact Explanation

An unprivileged ingress sender can:

1. **Force participation**: After a victim sends ICP to the swap subaccount (step 1) but before the victim calls `refresh_buyer_tokens` (step 2), the attacker calls `refresh_buyer_tokens(buyer=victim, confirmation_text=<correct text>)`. The victim's ICP is now committed to the swap. The victim cannot reclaim their ICP until the swap closes (via `error_refund_icp`), which may be days later.

2. **Bypass the confirmation-text consent mechanism**: The `confirmation_text` field is the only explicit consent signal in the participation flow. An SNS project may set a legal disclaimer or risk warning that users must acknowledge. Because any caller can supply this text on behalf of any buyer, the consent mechanism is completely undermined — a victim's ICP can be committed to a swap they never explicitly agreed to.

The two-step flow is explicitly documented as non-atomic: [5](#0-4) 

### Likelihood Explanation

The attack window exists for every user who has sent ICP to the swap subaccount but has not yet called `refresh_buyer_tokens`. This is a normal, expected intermediate state in the participation flow. The attacker needs only:
- Knowledge of the victim's principal (public on-chain)
- Knowledge of the confirmation text (publicly queryable from the swap canister's init)
- To submit an ingress message before the victim does

No privileged access, key material, or majority corruption is required. The attack is reachable by any unprivileged ingress sender.

### Recommendation

Validate that the caller is the buyer when a non-empty `buyer` field is supplied:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let caller = caller_principal_id();
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller
    } else {
        let requested = PrincipalId::from_str(&arg.buyer).unwrap();
        if requested != caller {
            panic!("Caller {} is not authorized to refresh tokens on behalf of {}", caller, requested);
        }
        requested
    };
    ...
}
```

This mirrors the fix applied in the CMC's `notify_create_canister`, which explicitly enforces `caller == controller` (with a narrow allowlist for the NNS-dapp backend): [6](#0-5) 

### Proof of Concept

1. SNS swap is open. SNS has set `confirmation_text = "I accept the risk"`.
2. Victim (`principal V`) sends 10 ICP to `swap_canister[subaccount = principal_to_subaccount(V)]` on the ICP ledger, intending to participate but not yet ready to confirm.
3. Attacker (`principal A`, any unprivileged user) queries the swap init to learn the confirmation text.
4. Attacker submits ingress:
   ```
   refresh_buyer_tokens({
     buyer: "<V's principal text>",
     confirmation_text: Some("I accept the risk")
   })
   ```
5. The swap canister resolves `p = V`, checks the balance at `swap_canister[subaccount=hash(V)]`, finds 10 ICP, validates the confirmation text (which the attacker supplied), and records V as a committed participant.
6. V's ICP is now locked in the swap. V never explicitly agreed to the confirmation text. V cannot reclaim ICP until the swap closes. [1](#0-0) [7](#0-6)

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

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L1117-1126)
```rust
pub struct RefreshBuyerTokensRequest {
    /// If not specified, the caller is used.
    #[prost(string, tag = "1")]
    pub buyer: ::prost::alloc::string::String,
    /// To accept the swap participation confirmation, a participant should send
    /// the confirmation text via refresh_buyer_tokens, matching the text set
    /// during SNS initialization.
    #[prost(string, optional, tag = "2")]
    pub confirmation_text: ::core::option::Option<::prost::alloc::string::String>,
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

**File:** rs/sns/test_utils/src/state_test_helpers.rs (L282-300)
```rust
    // First, transfer ICP to swap. Needs to go into a special subaccount...
    send_participation_funds(
        state_machine,
        swap_canister_id,
        participant_principal_id,
        amount,
    );

    // ... then, swap must be notified about that transfer.
    let response = state_machine
        .execute_ingress(
            swap_canister_id,
            "refresh_buyer_tokens",
            Encode!(&RefreshBuyerTokensRequest {
                buyer: participant_principal_id.to_string(),
                confirmation_text: None,
            })
            .unwrap(),
        )
```

**File:** rs/nns/cmc/src/main.rs (L1438-1474)
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
```
