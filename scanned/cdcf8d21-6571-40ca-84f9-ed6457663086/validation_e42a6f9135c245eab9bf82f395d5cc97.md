### Title
Missing Anonymous Principal Validation in SNS Swap `refresh_buyer_tokens` - (File: `rs/sns/swap/canister/canister.rs`)

### Summary
The `refresh_buyer_tokens` update endpoint in the SNS Swap canister accepts any syntactically valid principal string as the `buyer` argument — including the anonymous principal (`2vxsx-fae`) — without rejecting it. This is the direct IC analog of the missing `address(0)` check described in the external report: a function accepting an address-like argument without guarding against the "null" identity.

### Finding Description
In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` handler parses the caller-supplied `buyer` field and passes it directly to `refresh_buyer_token_e8s`: [1](#0-0) 

When `arg.buyer` is non-empty, the code does:
```rust
PrincipalId::from_str(&arg.buyer).unwrap()
```
with no subsequent check that the resulting `PrincipalId` is not the anonymous principal or the management canister principal.

The helper `is_valid_principal` in `rs/sns/swap/src/swap.rs` only verifies non-emptiness and parseability: [2](#0-1) 

The anonymous principal `"2vxsx-fae"` satisfies both conditions. The inner function `refresh_buyer_token_e8s` performs no identity check either — it immediately computes the buyer's subaccount and queries the ICP ledger: [3](#0-2) 

The `DirectInvestment::validate()` method, which does call `is_valid_principal`, is only invoked on stored neuron recipes, not during the live `refresh_buyer_tokens` call path: [4](#0-3) 

By contrast, the ckETH minter's analogous principal-parsing function explicitly rejects both the management canister principal (zero-length) and the anonymous principal: [5](#0-4) 

No equivalent guard exists in the SNS Swap buyer registration path.

### Impact Explanation
An unprivileged attacker can:
1. Transfer ICP to the anonymous principal's subaccount on the swap canister (computed via `principal_to_subaccount(&anonymous_principal)`).
2. Call `refresh_buyer_tokens` with `buyer = "2vxsx-fae"`.
3. The anonymous principal is registered as a legitimate buyer with the transferred ICP amount.
4. On swap commitment, `create_sns_neuron_basket_for_direct_participant` creates SNS neuron recipes for the anonymous principal: [6](#0-5) 

5. Those SNS neurons are permanently locked — nobody controls the anonymous principal, so the tokens are irrecoverable.

Additionally, if the swap has a maximum direct-participant cap (`MAX_NEURONS_FOR_DIRECT_PARTICIPANTS`), an attacker can consume participation slots with the anonymous principal, denying legitimate buyers entry to the swap.

### Likelihood Explanation
Low-to-medium. The attack is permissionless — any ingress sender can call `refresh_buyer_tokens` with an arbitrary `buyer` string. The attacker must spend real ICP to populate the anonymous principal's subaccount, making large-scale exploitation costly. However, the endpoint is publicly reachable with no authentication requirement, and the missing check is a single-line omission that any caller can exploit.

### Recommendation
Add an explicit rejection of the anonymous principal (and optionally the management canister principal) at the top of `refresh_buyer_tokens` in `rs/sns/swap/canister/canister.rs`, mirroring the guard already present in the ckETH minter's `parse_principal_from_slice`. For example:

```rust
if p == PrincipalId::new_anonymous() {
    panic!("anonymous principal is not allowed as a swap buyer");
}
```

Alternatively, extend `is_valid_principal` in `rs/sns/swap/src/swap.rs` to reject the anonymous principal and call it from the `refresh_buyer_tokens` handler before invoking `refresh_buyer_token_e8s`.

### Proof of Concept
1. An open SNS swap canister exists on a subnet.
2. Attacker computes the anonymous principal's subaccount: `principal_to_subaccount(&PrincipalId::new_anonymous())`.
3. Attacker transfers ICP to `Account { owner: swap_canister_id, subaccount: Some(<computed above>) }` on the ICP ledger.
4. Attacker sends an ingress update to the swap canister:
   ```
   refresh_buyer_tokens({ buyer = "2vxsx-fae", confirmation_text = null })
   ```
5. The call succeeds; the anonymous principal is now recorded in `swap.buyers` with the transferred ICP amount.
6. On swap finalization, `create_sns_neuron_basket_for_direct_participant` is invoked for `"2vxsx-fae"`, minting SNS neuron recipes whose `buyer_principal` is the anonymous principal — permanently locking those SNS tokens.

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

**File:** rs/sns/swap/src/swap.rs (L3269-3271)
```rust
pub fn is_valid_principal(p: &str) -> bool {
    !p.is_empty() && PrincipalId::from_str(p).is_ok()
}
```

**File:** rs/sns/swap/src/swap.rs (L3299-3351)
```rust
fn create_sns_neuron_basket_for_direct_participant(
    buyer_principal: &PrincipalId,
    amount_sns_token_e8s: u64,
    neuron_basket_construction_parameters: &NeuronBasketConstructionParameters,
    memo_offset: u64,
) -> Result<Vec<SnsNeuronRecipe>, String> {
    let mut recipes = vec![];

    let vesting_schedule =
        neuron_basket_construction_parameters.generate_vesting_schedule(amount_sns_token_e8s)?;

    let memo_of_longest_dissolve_delay = memo_offset + (vesting_schedule.len() - 1) as u64;
    let neuron_id_with_longest_dissolve_delay = SwapNeuronId::from(
        compute_neuron_staking_subaccount_bytes(*buyer_principal, memo_of_longest_dissolve_delay),
    );

    // Create the neuron basket for the direct investors. The unique
    // identifier for an SNS Neuron is the SNS Ledger Subaccount, which
    // is a hash of PrincipalId and some unique memo. Since direct
    // investors in the swap use their own principal_id, there are no
    // neuron id collisions, and each basket can use memos starting at memo_offset.
    for (i, scheduled_vesting_event) in vesting_schedule.iter().enumerate() {
        let memo = memo_offset + i as u64;
        // The SnsNeuronRecipes are set up such that all neurons in a basket will follow
        // the neuron with the longest dissolve delay
        let largest_dissolve_delay_neuron = i == vesting_schedule.len() - 1;
        let followees = if largest_dissolve_delay_neuron {
            vec![]
        } else {
            vec![neuron_id_with_longest_dissolve_delay.clone()]
        };

        recipes.push(SnsNeuronRecipe {
            sns: Some(TransferableAmount {
                amount_e8s: scheduled_vesting_event.amount_e8s,
                transfer_start_timestamp_seconds: 0,
                transfer_success_timestamp_seconds: 0,
                amount_transferred_e8s: Some(0),
                transfer_fee_paid_e8s: Some(0),
            }),
            investor: Some(Investor::Direct(DirectInvestment {
                buyer_principal: buyer_principal.to_string(),
            })),
            neuron_attributes: Some(NeuronAttributes {
                memo,
                dissolve_delay_seconds: scheduled_vesting_event.dissolve_delay_seconds,
                followees,
            }),
            claimed_status: Some(ClaimedStatus::Pending as i32),
        });
    }

    Ok(recipes)
```

**File:** rs/sns/swap/src/types.rs (L670-676)
```rust
impl DirectInvestment {
    pub fn validate(&self) -> Result<(), String> {
        if !is_valid_principal(&self.buyer_principal) {
            return Err(format!("Invalid principal {}", self.buyer_principal));
        }
        Ok(())
    }
```

**File:** rs/ethereum/cketh/minter/src/eth_logs/mod.rs (L260-291)
```rust
fn parse_principal_from_slice(slice: &[u8]) -> Result<Principal, String> {
    const ANONYMOUS_PRINCIPAL_BYTES: [u8; 1] = [4];

    if slice.is_empty() {
        return Err("slice too short".to_string());
    }
    if slice.len() > 32 {
        return Err(format!("Expected at most 32 bytes, got {}", slice.len()));
    }
    let num_bytes = slice[0] as usize;
    if num_bytes == 0 {
        return Err("management canister principal is not allowed".to_string());
    }
    if num_bytes > 29 {
        return Err(format!(
            "invalid number of bytes: expected a number in the range [1,29], got {num_bytes}",
        ));
    }
    if slice.len() < 1 + num_bytes {
        return Err("slice too short".to_string());
    }
    let (principal_bytes, trailing_zeroes) = slice[1..].split_at(num_bytes);
    if !trailing_zeroes
        .iter()
        .all(|trailing_zero| *trailing_zero == 0)
    {
        return Err("trailing non-zero bytes".to_string());
    }
    if principal_bytes == ANONYMOUS_PRINCIPAL_BYTES {
        return Err("anonymous principal is not allowed".to_string());
    }
    Principal::try_from_slice(principal_bytes).map_err(|err| err.to_string())
```
