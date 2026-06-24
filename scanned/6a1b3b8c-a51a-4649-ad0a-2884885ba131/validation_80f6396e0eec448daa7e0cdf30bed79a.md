### Title
Unauthorized Forced SNS Swap Participation on Behalf of Arbitrary Canister Freezes SNS Tokens - (File: rs/sns/swap/canister/canister.rs)

### Summary
The `refresh_buyer_tokens` endpoint in the SNS Swap canister accepts an arbitrary `buyer` principal from any caller without authorization checks. An unprivileged actor can send ICP to the swap canister's subaccount for a target canister and then call `refresh_buyer_tokens` specifying that canister as the buyer. When the swap commits, SNS neurons are created for the target canister. If the target canister does not implement `manage_neuron` logic, the SNS tokens are permanently frozen in the neuron.

### Finding Description

The `refresh_buyer_tokens` canister endpoint unconditionally accepts a caller-supplied `buyer` field and uses it as the participant principal:

```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // no authorization check
    };
    ...
    swap_mut().refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger).await
``` [1](#0-0) 

The inner `refresh_buyer_token_e8s` checks the ICP balance of `principal_to_subaccount(buyer)` on the swap canister and registers the buyer's participation: [2](#0-1) 

The proto definition explicitly documents this open design: [3](#0-2) 

When the swap commits, `sweep_sns` transfers SNS tokens to the neuron subaccount of each registered buyer, and `claim_swap_neurons` creates neurons controlled by those buyers: [4](#0-3) 

If the buyer is a canister that does not implement `manage_neuron` (e.g., `Disburse`, `StartDissolving`), the SNS tokens are permanently locked in the neuron with no recovery path.

### Impact Explanation

An attacker can:
1. Send ICP to `Account { owner: swap_canister, subaccount: principal_to_subaccount(VulnerableCanister) }` on the ICP ledger.
2. Call `refresh_buyer_tokens` with `buyer = VulnerableCanister`.
3. After swap commitment, `VulnerableCanister` receives SNS neurons it cannot manage.
4. SNS tokens are permanently frozen in the neuron — no rescue function exists in the SNS governance canister for this case.

The SNS token supply is effectively reduced. Any canister that does not implement SNS neuron management (the majority of deployed canisters) is a valid target. The `confirmation_text` field does not prevent this attack because it is publicly readable from the swap canister's state. [5](#0-4) 

### Likelihood Explanation

**Medium-Low.** The attacker must spend their own ICP (which is transferred to the SNS governance treasury). This economic cost limits opportunistic exploitation. However, a motivated attacker targeting a specific high-value canister (e.g., a DeFi protocol or DAO treasury canister) could execute this at relatively low cost. The attack requires no privileged access, no governance majority, and no threshold corruption — only an ingress call and an ICP ledger transfer.

### Recommendation

Add a caller authorization check in `refresh_buyer_tokens`: require that `caller == buyer` unless the caller is an explicitly allowlisted canister (analogous to how `notify_create_canister` restricts delegation to `NNS_DAPP_BACKEND_CANISTER_ID`): [6](#0-5) 

Alternatively, validate that the `buyer` field, if non-empty, matches the caller's principal, rejecting third-party registrations for arbitrary principals.

### Proof of Concept

```
// Attacker (any unprivileged principal) executes:

// Step 1: Send ICP to swap canister's subaccount for VulnerableCanister
icp_ledger.icrc1_transfer({
    to: { owner: SNS_SWAP_CANISTER, subaccount: principal_to_subaccount(VULNERABLE_CANISTER) },
    amount: min_participant_icp_e8s,
    ...
});

// Step 2: Register VulnerableCanister as a swap buyer
sns_swap.refresh_buyer_tokens({
    buyer: VULNERABLE_CANISTER.to_text(),
    confirmation_text: None,  // or the publicly-readable confirmation text
});

// After swap commits:
// - VulnerableCanister receives SNS neurons it cannot manage
// - SNS tokens are permanently frozen
// - Attacker's ICP goes to SNS governance treasury
```

The `buyer` field is accepted without any check that `caller == buyer`, as confirmed by the canister entry point at `rs/sns/swap/canister/canister.rs:130-133`. The `confirmation_text` (if set) is readable from the swap canister's public state via `get_sale_parameters`, so it does not serve as an effective barrier. [7](#0-6)

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

**File:** rs/sns/swap/src/swap.rs (L1593-1605)
```rust
        // Transfer the SNS tokens from the Swap canister.
        finalize_swap_response
            .set_sweep_sns_result(self.sweep_sns(now_fn, environment.sns_ledger()).await);
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        // Once SNS tokens have been distributed to the correct accounts, claim
        // them as neurons on behalf of the Swap participants.
        finalize_swap_response.set_claim_neuron_result(
            self.claim_swap_neurons(environment.sns_governance_mut())
                .await,
        );
```

**File:** rs/sns/swap/src/swap.rs (L2165-2200)
```rust
    pub async fn sweep_sns(
        &mut self,
        now_fn: fn(bool) -> u64,
        sns_ledger: &dyn ICRC1Ledger,
    ) -> SweepResult {
        if self.lifecycle() != Lifecycle::Committed {
            log!(
                ERROR,
                "Halting sweep_sns(). SNS Tokens cannot be distributed if \
                Lifecycle is not COMMITTED. Current Lifecycle: {:?}",
                self.lifecycle()
            );
            return SweepResult::new_with_global_failures(1);
        }

        let init = match self.init_and_validate() {
            Ok(init) => init,
            Err(error_message) => {
                log!(
                    ERROR,
                    "Halting sweep_sns(). State is missing or corrupted: {:?}",
                    error_message
                );
                return SweepResult::new_with_global_failures(1);
            }
        };

        // The following methods are safe to call since we validated Init in the above block
        let sns_governance = init.sns_governance_or_panic();
        let nns_governance = init.nns_governance_or_panic();
        let sns_transaction_fee_tokens = Tokens::from_e8s(init.transaction_fee_e8s_or_panic());

        let mut sweep_result = SweepResult::default();

        for recipe in self.neuron_recipes.iter_mut() {
            let neuron_memo = match recipe.neuron_attributes.as_ref() {
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

**File:** rs/nns/cmc/src/main.rs (L1438-1462)
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
```
