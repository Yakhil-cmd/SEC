### Title
Stale Treasury Valuation Used at Execution Time Enables Bypassing 7-Day Transfer Limit - (File: rs/sns/governance/src/proposal.rs)

### Summary
The SNS governance canister captures a treasury `Valuation` (token price in XDR) at proposal **submission** time and stores it in `action_auxiliary`. At proposal **execution** time, this stale valuation — which may be days old — is reused verbatim to compute the 7-day transfer upper bound. If the token price has dropped significantly since submission, the stale (higher) valuation permits a larger transfer than the current price would allow, undermining the treasury protection limit.

### Finding Description

When a `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal is submitted, `validate_and_render_transfer_sns_treasury_funds` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which fetches a live valuation and stores it in the proposal's `action_auxiliary`: [1](#0-0) 

This valuation is frozen at submission time and persisted in the proposal proto: [2](#0-1) 

At execution time, `perform_transfer_sns_treasury_funds` calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` passing the **stored** (stale) valuation directly: [3](#0-2) 

The execution-time check reuses this stale valuation to compute `allowance_tokens` without re-fetching the current price: [4](#0-3) 

The code itself acknowledges the staleness but only for the `spent_tokens` side, not the valuation: [5](#0-4) 

SNS governance proposals have a voting period that can extend up to `initial_voting_period_seconds + 2 * wait_for_quiet_deadline_increase_seconds`. With default parameters this can be several days. During this window, the token price can drop substantially. The valuation used to compute the XDR-denominated limit (`MAX_XDR = 300,000`) is the one from submission, not execution.

The `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens` function converts the XDR cap into tokens using the **submission-time** `xdrs_per_icp` and `icps_per_token`: [6](#0-5) 

If the token price at submission was 10 XDR/token and the cap is 300,000 XDR, the allowed transfer is 30,000 tokens. If by execution time the price has fallen to 1 XDR/token, the actual value transferred is only 30,000 XDR — but the stale valuation still permits 30,000 tokens, which at the new price represents only 30,000 XDR. In the reverse scenario (price rises), the stale low valuation allows more tokens than the current price cap would permit, enabling a transfer worth far more than 300,000 XDR at execution time.

### Impact Explanation

An SNS neuron holder (unprivileged governance participant) can submit a `TransferSnsTreasuryFunds` proposal during a period of temporarily elevated token price. If the price subsequently rises further between submission and execution (e.g., due to market movement or coordinated manipulation), the stale high-price valuation allows a token quantity that, at execution time, exceeds the intended 300,000 XDR cap. This directly undermines the treasury protection mechanism designed to limit how much value can be extracted from an SNS treasury within a 7-day window.

Conversely, a proposer can time submission when the token price is high, then wait for execution when the price is lower — the stale valuation still permits the larger token quantity, draining more tokens than the current price-based limit would allow.

### Likelihood Explanation

Any SNS governance participant with sufficient staked tokens to submit a proposal can trigger this. SNS token prices are volatile. The voting period for SNS proposals is configurable and can be multiple days. The gap between submission and execution is a normal, expected part of the governance lifecycle — no special network conditions are required. The attacker only needs to observe market prices and time their proposal submission accordingly.

### Recommendation

Re-fetch a fresh valuation at execution time instead of reusing the submission-time valuation. Replace the stored `valuation` passed to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` with a live call to `assess_treasury_balance` inside `perform_transfer_sns_treasury_funds`. The stored `action_auxiliary` valuation can be retained for audit/display purposes but should not be the sole input to the execution-time limit check.

Alternatively, enforce a maximum age on the stored valuation: if `now - valuation.timestamp_seconds` exceeds a threshold (e.g., 24 hours), reject execution and require the proposer to resubmit.

### Proof of Concept

1. SNS token is trading at 10 XDR/token. Treasury holds 1,000,000 tokens (10,000,000 XDR total). The 7-day cap is `min(25% of treasury, 300,000 XDR)` = 300,000 XDR = 30,000 tokens at submission price.
2. Attacker submits a `TransferSnsTreasuryFunds` proposal for 30,000 tokens. Submission-time valuation is stored: `xdrs_per_icp * icps_per_token = 10 XDR/token`.
3. Proposal passes after the voting period (e.g., 4 days).
4. By execution time, token price has risen to 20 XDR/token.
5. `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` computes `allowance_tokens` using the stale 10 XDR/token valuation → 30,000 tokens allowed.
6. The transfer executes: 30,000 tokens × 20 XDR/token = **600,000 XDR** is extracted — double the intended 300,000 XDR cap.

The stale valuation path is confirmed at: [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L570-578)
```rust
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1063-1065)
```text
  message TransferSnsTreasuryFundsActionAuxiliary {
    Valuation valuation = 1;
  }
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L66-113)
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
```
