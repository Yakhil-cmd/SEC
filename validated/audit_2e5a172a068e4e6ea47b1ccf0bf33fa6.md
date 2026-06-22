### Title
Concurrent Inter-Canister Reads of Treasury Balance and Total Supply Enable SNS Valuation Manipulation - (`rs/sns/governance/token_valuation/src/lib.rs`)

---

### Summary

`try_get_balance_valuation_factors` uses `join!` to concurrently fetch `icrc1_balance_of` (treasury balance) and `icrc1_total_supply` (used in the price formula) from the same SNS ledger canister in separate inter-canister calls. Because these calls are not atomic, the ledger state can change between them. An attacker who burns SNS tokens in the window between the two calls causes `current_supply` to be understated relative to `treasury_balance`, inflating the computed `icps_per_token` and therefore the treasury valuation. An inflated valuation raises the 7-day `TransferSnsTreasuryFunds` upper bound, allowing a larger treasury drain than the protocol intends.

---

### Finding Description

`try_get_balance_valuation_factors` in `rs/sns/governance/token_valuation/src/lib.rs` fires three futures concurrently: [1](#0-0) 

```
join!(
    icrc1_balance_of(treasury_account),   // → SNS ledger
    icps_per_token_client.get(),           // → internally calls fetch_icps_per_sns_token
    xdrs_per_icp_client.get(),             // → CMC
)
```

`icps_per_token_client.get()` resolves by calling `fetch_icps_per_sns_token`, which itself fires three more concurrent calls: [2](#0-1) 

```
join!(
    get_derived_state(swap_canister),          // initial price
    initial_supply_e8s(sns_ledger),            // historical first-mint amount
    icrc1_total_supply(sns_ledger),            // current supply
)
```

The final valuation formula is: [3](#0-2) 

```
icps_per_token = initial_icps_per_sns_token / (current_supply / initial_supply)
valuation_xdr  = treasury_balance * icps_per_token * xdrs_per_icp
```

`treasury_balance` comes from `icrc1_balance_of` and `current_supply` comes from `icrc1_total_supply`. Both are sent to the SNS ledger in the same IC round, but the ledger processes them sequentially. Any message that the ledger processes **between** those two calls (e.g., a burn) causes the two values to reflect different ledger states.

This valuation is computed at proposal-submission time and stored in `TransferSnsTreasuryFundsActionAuxiliary`: [4](#0-3) 

It is then used at execution time to enforce the 7-day upper bound: [5](#0-4) 

The upper-bound tiers are: [6](#0-5) 

- **Small** (< 100 000 XDR): `NoLimit` — any amount
- **Medium** (100 000 – 1 200 000 XDR): 25 % of treasury
- **Large** (> 1 200 000 XDR): 300 000 XDR cap

Inflating the valuation across a tier boundary directly raises the permitted transfer ceiling.

---

### Impact Explanation

An attacker who can burn SNS tokens (any token holder can call `icrc1_transfer` to the minting account, which burns) can race a burn against the concurrent ledger calls made during `TransferSnsTreasuryFunds` proposal submission:

1. `icrc1_balance_of(treasury)` is processed → returns balance **B** (burn not yet applied).
2. Attacker's burn is processed → `total_supply` drops by **Δ**.
3. `icrc1_total_supply` is processed → returns **S − Δ** instead of **S**.

Result: `icps_per_token = initial_price × initial_supply / (S − Δ)` is inflated by factor `S / (S − Δ)`. For a treasury sitting just below the 1 200 000 XDR "large" threshold, a modest burn can push the apparent valuation above it, raising the allowed 7-day transfer from 25 % of treasury to 300 000 XDR — a net gain of tens of thousands of XDR worth of SNS tokens at the cost of the burned tokens. For a treasury near the 100 000 XDR "medium" threshold, the same trick can push it into the "small" (`NoLimit`) regime, removing all per-transfer constraints.

The stored (manipulated) valuation is then used verbatim at execution time without re-querying the ledger: [7](#0-6) 

---

### Likelihood Explanation

The attacker controls the timing of their own burn transaction. On the IC, all inter-canister calls sent in the same round by the governance canister arrive at the SNS ledger in the same round. The ledger processes them in deterministic order. The attacker submits a burn in the same round as the governance canister's proposal-validation call. Because the ledger queues messages from multiple callers, the burn can land between `icrc1_balance_of` and `icrc1_total_supply` processing. The attacker can repeat the attempt across multiple proposal submissions at negligible cost (only the burned tokens are lost, and those can be sized to be smaller than the gain). The attack does not require any privileged role — any SNS token holder can burn their own tokens.

---

### Recommendation

1. **Atomic snapshot**: Add a single ledger endpoint (or use a composite query) that returns `(balance_of, total_supply)` atomically in one call, eliminating the inter-call window.
2. **Sequential reads**: Replace the `join!` with sequential `await` calls so that `icrc1_balance_of` and `icrc1_total_supply` are always read from the same ledger state.
3. **Re-validate at execution**: Re-fetch a fresh valuation at execution time (inside `perform_transfer_sns_treasury_funds`) rather than relying solely on the submission-time snapshot, so a manipulated snapshot cannot survive to execution.

---

### Proof of Concept

```
Attacker.burn_sns_tokens() →
    // Submit burn to SNS ledger in the same IC round as the governance
    // canister's proposal-validation inter-canister calls.
    SNSLedger.icrc1_transfer({ to: minting_account, amount: LARGE_DELTA })

// Concurrent calls from governance canister (same round):
SNSGovernance.make_proposal(TransferSnsTreasuryFunds { amount: LARGE }) →
    assess_treasury_balance() →
        try_get_balance_valuation_factors() →
            join!(
                // Processed first by ledger → returns balance B (burn not yet applied)
                icrc1_balance_of(treasury),

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L154-163)
```rust
    let balance_of_request = icrc1_client.icrc1_balance_of(account);
    let icps_per_token_request = icps_per_token_client.get();
    let xdrs_per_icp_request = xdrs_per_icp_client.get();

    // Make all (3) requests (concurrently).
    let (balance_of_response, icps_per_token_response, xdrs_per_icp_response) = join!(
        balance_of_request,
        icps_per_token_request,
        xdrs_per_icp_request,
    );
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L316-330)
```rust
        let (get_derived_state_result, initial_supply_e8s_result, current_supply_result) = join!(
            // 1. SNS token price from swap.
            call::<_, MyRuntime>(self.swap_canister_id, GetDerivedStateRequest {}),
            // 2. Initial SNS token supply.
            initial_supply_e8s::<MyRuntime>(
                self.sns_token_ledger_canister_id,
                InitialSupplyOptions::new()
            ),
            // 3. Current SNS token supply.
            MyRuntime::call_with_cleanup::<_, (Nat,)>(
                self.sns_token_ledger_canister_id,
                "icrc1_total_supply",
                ()
            ),
        );
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L397-414)
```rust
        let total_inflation = current_supply_e8s
            .checked_div(initial_supply_e8s)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to perform current_supply / initial_supply \
                     (where current_supply_e8s = {current_supply_e8s} and initial_supply_e8s = {initial_supply_e8s})",
                ))
            })?;

        // Finally, current price = initial price scaled down by inflation (or deflation).
        initial_icps_per_sns_token
            .checked_div(total_inflation)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to perform initial_icps_per_sns_token / total_inflation \
                     (where initial_icps_per_sns_token = {initial_icps_per_sns_token} and total_inflation = {total_inflation})",
                ))
            })
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L1502-1505)
```rust
    pub struct TransferSnsTreasuryFundsActionAuxiliary {
        #[prost(message, optional, tag = "1")]
        pub valuation: ::core::option::Option<super::Valuation>,
    }
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2656)
```rust
pub(crate) fn transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err<'a>(
    transfer: &TransferSnsTreasuryFunds,
    valuation: Valuation,
    proposals: impl Iterator<Item = &'a ProposalData>,
    now_timestamp_seconds: u64,
) -> Result<(), GovernanceError> {
    let allowance_tokens = transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(valuation)
        .map_err(|err| {
            // This should not be possible, because valuation was already used the same way during
            // proposal submission/creation/validation.
            GovernanceError::new_with_message(
                ErrorType::InconsistentInternalData,
                format!(
                    "Unable to determined upper bound on the amount of \
                     TransferSnsTreasuryFunds proposals: {err:?}\nvaluation:{valuation:?}",
                ),
            )
        })?;

    // The total calculated here _could_ be different from what was calculated at proposal
    // submission/creation time. A difference would result from the execution of (another)
    // TransferSnsTreasuryFunds proposal between now and then.
    let spent_tokens = total_treasury_transfer_amount_tokens(
        proposals,
        transfer.from_treasury(),
        now_timestamp_seconds - 7 * ONE_DAY_SECONDS,
    )
    .map_err(|message| {
        GovernanceError::new_with_message(ErrorType::InconsistentInternalData, message)
    })?;

    let remainder_tokens = allowance_tokens - spent_tokens;
    let transfer_amount_tokens = denominations_to_tokens(transfer.amount_e8s, E8)
        // This Err cannot be provoked, because we are dividing a u64 (amount_e8s) by a positive
        // integer (E8).
        .ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::UnreachableCode,
                format!(
                    "Unable to convert proposals amount {} e8s to tokens.",
                    transfer.amount_e8s,
                ),
            )
        })?;
    if transfer_amount_tokens > remainder_tokens {
        return Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Executing this proposal is not allowed at this time, because doing \
                 so would cause the 7 day upper bound of {allowance_tokens} tokens to be exceeded. \
                 Maybe, try again later? The total amount transferred in the past \
                 7 days stands at {spent_tokens} tokens, and the amount in this proposal is {transfer_amount_tokens} \
                 tokens. The upper bound is based on treasury valuation factors at \
                 the time of proposal submission: {valuation:?}",
            ),
        ));
    }
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L34-41)
```rust
impl ProposalsAmountTotalUpperBound {
    // A treasury can be small, medium, or large. These are the boundaries between those regimes.
    const MAX_SMALL_TREASURY_SIZE_XDR: Decimal = dec!(100_000);
    const MAX_MEDIUM_TREASURY_SIZE_XDR: Decimal = dec!(1_200_000);

    // No matter how large the treasury is, not more than this amount can be removed (within a 7 day
    // window).
    const MAX_XDR: Decimal = dec!(300_000);
```

**File:** rs/sns/governance/src/governance.rs (L3000-3005)
```rust
        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;
```
