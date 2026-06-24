### Title
SNS Treasury Transfer Limit Enforced Using Stale Submission-Time Token Valuation — (`rs/sns/governance/src/proposal.rs`, `rs/sns/governance/proposals_amount_total_limit/src/lib.rs`)

---

### Summary

The SNS governance canister captures a treasury `Valuation` snapshot (token balance × ICPs-per-token × XDRs-per-ICP) at **proposal submission time** and stores it in `ProposalData.action_auxiliary`. At **execution time**, the same stale snapshot is reused to compute the 7-day transfer allowance limit. If the SNS token price changes materially between submission and execution (the voting period is typically several days), the financial safety limit is computed against an outdated price, allowing the actual XDR value transferred to exceed the intended cap — or, in the "NoLimit" regime, to bypass the cap entirely.

---

### Finding Description

**Valuation capture (submission time):**

`validate_and_render_transfer_sns_treasury_funds` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which calls `assess_treasury_balance` to fetch a live valuation and stores it in `ActionAuxiliary::TransferSnsTreasuryFunds(valuation)`. [1](#0-0) 

**Valuation reuse (execution time):**

`perform_transfer_sns_treasury_funds` passes the stored `valuation?` (the submission-time snapshot) directly to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`. No fresh price is fetched. [2](#0-1) 

**Limit computation from stale valuation:**

`ProposalsAmountTotalUpperBound::in_tokens` classifies the treasury as *small* (≤ 100,000 XDR → **NoLimit**), *medium* (≤ 1,200,000 XDR → 25% fraction), or *large* (> 1,200,000 XDR → 300,000 XDR cap). This classification uses the stale `valuation.to_xdr()`. [3](#0-2) 

The code itself acknowledges the staleness: [4](#0-3) 

**SNS token price proxy:**

For SNS token treasuries, `IcpsPerSnsTokenClient` derives the current price from the **initial swap price** (`sns_tokens_per_icp` from `get_derived_state`) adjusted for supply inflation — not from any live market oracle. This proxy can diverge substantially from the actual market price. [5](#0-4) 

There is a `MIN_XDRS_PER_ICP` floor of 1 XDR to prevent artificially low valuations from inflating the allowance, but there is **no `MAX_XDRS_PER_ICP`** and **no clamping of `icps_per_token`**. A very low `icps_per_token` at submission time (e.g., token price near zero) places the treasury in the **NoLimit** regime. [6](#0-5) 

---

### Impact Explanation

**Scenario — NoLimit bypass:**

1. SNS token price is very low at submission time → treasury XDR value < 100,000 → `NoLimit` (any amount can be transferred).
2. Proposal passes voting (4–8 day window).
3. SNS token price rises 10× before execution → treasury is now worth > 1,200,000 XDR → should be capped at 300,000 XDR.
4. Execution proceeds with `NoLimit` from the stale valuation → the full treasury can be drained in a single proposal.

**Scenario — Fraction/Cap bypass:**

1. At submission, treasury is "medium" → 25% fraction limit computed in tokens using the low submission-time price.
2. Token price rises before execution → the same token count is now worth far more XDR than the 25% limit was intended to allow.
3. The stale token-denominated limit is not re-evaluated against the current price.

**Impact:** High — the 7-day treasury transfer safety limit, which is the primary on-chain protection against rapid SNS treasury drainage, can be bypassed or rendered ineffective by a price movement between proposal submission and execution.

---

### Likelihood Explanation

**Low-Medium.** Requires:
- An SNS token price that is low at submission time and rises significantly before execution (or vice versa).
- A neuron holder with sufficient stake to submit and pass a `TransferSnsTreasuryFunds` proposal.
- No external intervention (e.g., voters rejecting the proposal after observing the price change).

SNS token prices are volatile, and voting periods of 4+ days are standard. The scenario is realistic for any SNS with a volatile token. The attacker does not need any privileged access — any neuron holder can submit proposals.

---

### Recommendation

At execution time, re-fetch a fresh valuation rather than reusing the submission-time snapshot, or re-evaluate the treasury regime (small/medium/large) using the current price. If a fresh valuation cannot be obtained (e.g., external call fails), the execution should be deferred rather than proceeding with a stale limit. Alternatively, apply the **more restrictive** of the submission-time and execution-time valuations.

---

### Proof of Concept

**Entry path (unprivileged ingress):**

1. Any SNS neuron holder calls `manage_neuron` → `MakeProposal` → `TransferSnsTreasuryFunds` on the SNS governance canister when the SNS token price is low (treasury XDR value < 100,000 → `NoLimit`).
2. The proposal is accepted; `validate_and_render_transfer_sns_treasury_funds` captures the low-price valuation in `action_auxiliary`.
3. Voters approve the proposal over the voting period.
4. The SNS token price rises 10× during the voting period.
5. `perform_transfer_sns_treasury_funds` is called; it passes the stale `valuation` (from step 2) to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`.
6. `ProposalsAmountTotalUpperBound::from_valuation_xdr` receives the stale low XDR value → returns `NoLimit`.
7. The full treasury transfer executes with no token-amount cap, even though the actual XDR value now far exceeds the intended 300,000 XDR safety ceiling. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L554-578)
```rust
async fn validate_and_render_transfer_sns_treasury_funds(
    transfer: &TransferSnsTreasuryFunds,
    sns_transfer_fee_e8s: u64,
    env: &dyn Environment,
    swap_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
) -> Result<
    (
        String, // Rendering.
        ActionAuxiliary,
    ),
    String,
> {
    let mut defects = vec![];

    // Validate amount. This requires calling CMC and the swap canister; hence, await.
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        transfer,
    )
    .await;
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2617)
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
```

**File:** rs/sns/governance/src/proposal.rs (L2644-2655)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L2980-3005)
```rust
    async fn perform_transfer_sns_treasury_funds(
        &mut self,
        proposal_id: u64, // This is just to control concurrency.
        valuation: Result<Valuation, GovernanceError>,
        transfer: &TransferSnsTreasuryFunds,
    ) -> Result<(), GovernanceError> {
        // Only execute one proposal of this type at a time.
        thread_local! {
            static IN_PROGRESS_PROPOSAL_ID: RefCell<Option<u64>> = const { RefCell::new(None) };
        }
        let release_on_drop = acquire(&IN_PROGRESS_PROPOSAL_ID, proposal_id);
        if let Err(already_in_progress_proposal_id) = release_on_drop {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Another TransferSnsTreasuryFunds proposal (ID = {already_in_progress_proposal_id}) is already in progress.",
                ),
            ));
        }

        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L43-64)
```rust
    /// A price quote less than this is considered "unrealistically" low. When that happens, we use
    /// this instead of the quoted value.
    ///
    /// # Motivation
    ///
    /// Low XDRs per ICP quotes would tend to cause our valuations to be in the "small" regime,
    /// where an SNS is allowed to take the biggest actions relative to their size. This is to
    /// minmize the damage caused by wacky price quotes.
    ///
    /// # What Value to Use
    ///
    /// Currently, the minimum XDRs per ICP used by NNS governance is 1. This is simply copied from
    /// there, specifically from the minimum_icp_xdr_rate field in NetworkEconomics.
    ///
    /// As of Mar 2024, the price of ICP is around 10 XDR. The lowest it has ever been is around 2.2
    /// XDR. FWIW, this is less than that.
    ///
    /// # Why Not Also Define MAX?
    ///
    /// Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our
    /// valuations to be in the "large" regime, where actions are more limited.
    const MIN_XDRS_PER_ICP: Decimal = dec!(1);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L66-135)
```rust
    fn in_tokens(mut valuation: Valuation) -> Result<Decimal, ProposalsAmountTotalLimitError> {
        Self::clamp_xdrs_per_icp(&mut valuation);

        let ValuationFactors {
            tokens: balance_tokens,
            icps_per_token,
            xdrs_per_icp,
        } = valuation.valuation_factors;

        let self_ = Self::from_valuation_xdr(valuation.to_xdr());
        let result_tokens = match self_ {
            Self::NoLimit => balance_tokens,

            Self::Fraction(fraction) => balance_tokens
                .checked_mul(fraction)
                // Overflow should not be possible, since fraction is supposed to be at most 1.0.
                .ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "Unable to perform {balance_tokens} * {fraction}.",
                    ))
                })?,

            Self::Xdr(max_xdr) => {
                let xdrs_per_token = xdrs_per_icp.checked_mul(icps_per_token).ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "XDRs per token could not be calculated from valuation: {valuation:?}"
                    ))
                })?;

                // Calculate the inverse conversion rate.
                if xdrs_per_token == Decimal::from(0) {
                    // This is not reachable, because in this case, valuation.to_xdr() would return
                    // 0, and in that case, we would have taken the NoLimit branch.
                    return Err(ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "It appears that the tokens have zero value in XDR. valuation = {valuation:?}"
                    )));
                }
                let tokens_per_xdr = xdrs_per_token.inv();

                max_xdr.checked_mul(tokens_per_xdr).ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "Max tokens could not be calculated with valuation: {valuation:?}",
                    ))
                })?
            }
        };

        Ok(result_tokens)
    }

    fn from_valuation_xdr(valuation_xdr: Decimal) -> Self {
        // Ideally, this would be checked at compile time. In principal, this should be possible,
        // since all the inputs are const, but I'm not sure how to do that. Therefore,
        // debug_assert_eq is used instead, and should be very nearly as good, because this will be
        // run during CI.
        debug_assert_eq!(
            Self::MAX_MEDIUM_TREASURY_SIZE_XDR.checked_mul(ONE_QUARTER),
            Some(Self::MAX_XDR),
        );

        if valuation_xdr <= Self::MAX_SMALL_TREASURY_SIZE_XDR {
            return Self::NoLimit;
        }

        if valuation_xdr <= Self::MAX_MEDIUM_TREASURY_SIZE_XDR {
            return Self::Fraction(ONE_QUARTER);
        }

        Self::Xdr(Self::MAX_XDR)
    }
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L314-415)
```rust
    async fn fetch_icps_per_sns_token(&self) -> Result<Decimal, ValuationError> {
        // (Concurrently) fetch the various pieces that we need to sythensize the result:
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
        // (Factors 2 and 3 tell us how much inflation there has been. For
        // example, if the amount of tokens has doubled since the beginning,
        // then the current ICPs per SNS token should be half of what it was at
        // the time of the swap.)

        // Unwrap (intermediate) results.
        let get_derived_state_response = get_derived_state_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to obtain SNS token price at the time of the SNS initialization swap: {err:?}",
            ))
        })?;
        let initial_supply_e8s = initial_supply_e8s_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to determine the initial supply of SNS tokens: {err:?}",
            ))
        })?;
        let (current_supply_e8s,) = current_supply_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to obtain the current supply of SNS tokens: {err:?}",
            ))
        })?;

        // Read the relevant fields.

        // Here, a floating point field is used. This is ok, because we are just
        // using this to come up with a valuation, which isn't an exact science.
        let initial_sns_tokens_per_icp: f64 = get_derived_state_response
            .sns_tokens_per_icp
            .ok_or_else(|| {
                ValuationError::new_mismatch(format!(
                    "Response from swap ({}) get_derived_state call did not \
                     contain sns_tokens_per_icp: {:#?}",
                    self.swap_canister_id, get_derived_state_response,
                ))
            })?;

        // Convert all numbers to Decimal.

        let initial_sns_tokens_per_icp = Decimal::from_f64_retain(initial_sns_tokens_per_icp)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to convert sns_tokens_per_icp {initial_sns_tokens_per_icp} (double precision \
                     floating point) to Decimal.",
                ))
            })?;

        let initial_supply_e8s = i2d(initial_supply_e8s);

        let current_supply_e8s =
            Decimal::from(current_supply_e8s.0.to_u128().ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to convert current_supply_e8s ({current_supply_e8s}) from Nat to Decimal.",
                ))
            })?);

        // Do actual (simple) math.

        // Flip the ratio from SNS tokens per ICP to ICPs per SNS token.
        let initial_icps_per_sns_token = Decimal::from(1)
            .checked_div(initial_sns_tokens_per_icp)
            .ok_or_else(|| {
            ValuationError::new_arithmetic(format!(
                "Unable to perform 1 / sns_tokens_per_icp (where sns_tokens_per_icp = {initial_sns_tokens_per_icp}).",
            ))
        })?;

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
    }
```
