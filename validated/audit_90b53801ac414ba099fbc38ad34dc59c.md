### Title
Unprivileged Caller Can Force Any Principal's SNS Swap Participation via `refresh_buyer_tokens` - (File: rs/sns/swap/canister/canister.rs)

### Summary
The `refresh_buyer_tokens` endpoint in the SNS Swap canister accepts an arbitrary `buyer` principal from the caller without verifying that the caller is authorized to act on behalf of that buyer. Any unprivileged ingress sender can force another user's ICP into a committed swap state, permanently preventing that user from reclaiming their ICP via `error_refund_icp`.

### Finding Description
In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` update method resolves the effective buyer principal from the caller-supplied `arg.buyer` string field:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← attacker-controlled
    };
    ...
    swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
``` [1](#0-0) 

When `arg.buyer` is non-empty, the caller-supplied principal is used verbatim. No check is made that the caller equals the buyer or holds any delegation. The inner `refresh_buyer_token_e8s` then reads the ICP ledger balance of `swap_subaccount(buyer)` and writes a `BuyerState` entry keyed by that principal:

```rust
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),
};
icp_ledger.account_balance(account).await
``` [2](#0-1) 

Once a `BuyerState` record exists for a principal, that principal's ICP is owned by the swap and swept (committed to SNS governance or refunded) only when the swap finalises. The `error_refund_icp` escape hatch — which lets a user reclaim ICP they sent but never registered — is no longer available to them. [3](#0-2) 

The `BuyerState` struct and the `buyers` map confirm that the key is the buyer's principal string, with no record of who triggered the registration: [4](#0-3) [5](#0-4) 

The `confirmation_text` field does not mitigate this: it is a public swap parameter that any attacker can read from `get_sale_parameters` before crafting the call. [6](#0-5) 

### Impact Explanation
A victim (Alice) who has transferred ICP to `swap_subaccount(Alice)` but has not yet called `refresh_buyer_tokens` retains the ability to reclaim those funds via `error_refund_icp` once the swap closes. An attacker (Eve) who calls `refresh_buyer_tokens(buyer = Alice)` before Alice does irrevocably registers Alice's ICP into the swap. If the swap commits, Alice receives SNS tokens she did not choose to acquire; she loses the ability to exit cleanly. The attacker spends only the cycles cost of one update call and can automate this against every address observed sending ICP to the swap canister's subaccounts.

### Likelihood Explanation
The swap canister's subaccount scheme is deterministic and public: `subaccount = principal_to_subaccount(buyer)`. An attacker can monitor the ICP ledger for transfers whose destination matches any `swap_subaccount(P)` pattern, extract `P`, and immediately call `refresh_buyer_tokens(buyer = P)`. No privileged access, key material, or social engineering is required. The attack is fully automatable from an ordinary ingress sender.

### Recommendation
- **Short term**: In `refresh_buyer_tokens`, when `arg.buyer` is non-empty, assert `caller_principal_id() == p` before proceeding. If third-party helpers (e.g., NNS dapp) legitimately need to call on behalf of others, maintain an explicit allowlist of trusted callers, analogous to `authorize_caller_to_call_notify_create_canister_on_behalf_of_creator` in the CMC. [7](#0-6) 

- **Long term**: Document the valid state-transition graph for the swap lifecycle and enforce that every state-mutating endpoint validates the initiator's identity, not just the existence of on-chain funds.

### Proof of Concept
1. Alice transfers 100 ICP to `AccountIdentifier(swap_canister, subaccount(Alice))` on the ICP ledger, intending to participate but waiting to confirm the swap terms.
2. Eve observes this transfer on the public ledger, extracts Alice's principal, and immediately calls:
   ```
   refresh_buyer_tokens({ buyer: "<Alice's principal>", confirmation_text: <public swap text> })
   ```
   from any identity on the swap canister.
3. `refresh_buyer_token_e8s` reads Alice's 100 ICP balance, writes `buyers["Alice"] = BuyerState { icp: 100 ICP }`, and returns success.
4. Alice's `BuyerState` is now set. `error_refund_icp` will no longer return Alice's ICP because her funds are tracked in `buyers`.
5. If the swap commits, Alice receives SNS tokens she did not choose to acquire and cannot recover her ICP. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L111-115)
```rust
/// Get Params.
#[query]
fn get_sale_parameters(request: GetSaleParametersRequest) -> GetSaleParametersResponse {
    swap().get_sale_parameters(&request)
}
```

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

**File:** rs/sns/swap/src/swap.rs (L1126-1163)
```rust
    /// an error).
    ///
    /// If a ledger transfer was successfully made, but this call
    /// fails (many reasons are possible), the owner of the ICP sent
    /// to the subaccount can reclaim their tokens using `error_refund_icp`
    /// once this swap is closed (committed or aborted).
    ///
    /// TODO(NNS1-1682): attempt to refund ICP that cannot be accepted.
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

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L216-219)
```text
  //
  // The key is the textual representation of the buyer's principal
  // and the value represents the bid.
  map<string, BuyerState> buyers = 6;
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L656-687)
```text
message BuyerState {
  reserved 1 to 4;
  // The amount of ICP accepted from this buyer. ICP is accepted by
  // first making a ledger transfer and then calling the method
  // `refresh_buyer_token_e8s`.
  //
  // Can only be set when a buyer state record for a new buyer is
  // created, which can only happen when the lifecycle state is
  // `Open`. Must be at least `min_participant_icp_e8s`, and at most
  // `max_participant_icp_e8s`.
  //
  // Invariant between canisters in the OPEN state:
  //
  //  ```text
  //  icp.amount_e8 <= icp_ledger.balance_of(subaccount(swap_canister, P)),
  //  ```
  //
  // where `P` is the principal ID associated with this buyer's state.
  //
  // ownership
  // * PENDING - a `BuyerState` cannot exist
  // * OPEN - owned by the buyer, cannot be transferred out
  // * COMMITTED - owned by the SNS governance canister, can be transferred out
  // * ABORTED - owned by the buyer, can be transferred out
  TransferableAmount icp = 5;

  // Idempotency flag indicating whether the neuron recipes have been created for
  // the BuyerState. When set to true, it signifies that the action of creating neuron
  // recipes has been performed on this structure. If the action is retried, this flag
  // can be checked to avoid duplicate operations.
  optional bool has_created_neuron_recipes = 6;
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
