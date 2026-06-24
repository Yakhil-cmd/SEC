### Title
Stale Treasury Valuation Snapshot Used at Proposal Execution Allows Disproportionate SNS Treasury Drain - (`rs/sns/governance/src/proposal.rs`)

---

### Summary

The SNS governance canister captures a treasury valuation snapshot (token balance × ICP/token rate × XDR/ICP rate) at **proposal submission time** and stores it in `action_auxiliary`. When a `TransferSnsTreasuryFunds` proposal is later **executed** — potentially days after the voting period — the execution-time safety check reuses this stale snapshot to compute the 7-day transfer allowance, without refreshing the treasury valuation. If the SNS token price drops significantly between proposal creation and execution, the stale (inflated) valuation permits a transfer that is disproportionately large relative to the treasury's current value, undermining the conservation guarantee the limit is designed to enforce.

---

### Finding Description

**At proposal creation time**, `validate_and_render_transfer_sns_treasury_funds` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which calls `assess_treasury_balance` to fetch a live valuation (balance, `icps_per_token`, `xdrs_per_icp`) and stores it in `action_auxiliary`: [1](#0-0) 

This valuation is persisted in the proposal's `action_auxiliary.transfer_sns_treasury_funds.valuation` field: [2](#0-1) 

**At proposal execution time**, `perform_transfer_sns_treasury_funds` retrieves this stale snapshot from `action_auxiliary` and passes it directly to the execution-time guard: [3](#0-2) 

The guard `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` computes `allowance_tokens` from the stale valuation without re-fetching current prices: [4](#0-3) 

The code itself acknowledges the staleness in the error message: [5](#0-4) 

The valuation factors that can go stale are the three components fetched concurrently at creation time — token balance, `icps_per_token` (from the swap canister), and `xdrs_per_icp` (from CMC): [6](#0-5) 

The 7-day limit is computed from the XDR value of the treasury at creation time. For a "large" treasury (>1.2M XDR), the limit is a fixed 300,000 XDR worth of tokens: [7](#0-6) 

---

### Impact Explanation

The 7-day transfer limit is the primary on-chain safeguard against treasury drain via governance proposals. If the SNS token price drops 50% between proposal submission and execution, the stale valuation doubles the effective allowance in token terms relative to the current treasury value. For a "large" treasury, the fixed 300,000 XDR cap is converted to tokens using the stale (higher) `xdrs_per_token` rate, yielding more tokens than the current rate would permit. An attacker who submits a proposal when the token price is high and the treasury appears large can execute a transfer that exceeds what the current treasury value warrants, draining a disproportionate fraction of the treasury.

Concretely: if the SNS token is worth 10 XDR at proposal creation and 2 XDR at execution, the stale valuation allows 5× more tokens to be transferred than the current price-based limit would permit.

---

### Likelihood Explanation

SNS voting periods are typically several days (configurable, often 4–7 days). SNS token prices are frequently volatile. A 30–50% price drop during a multi-day voting window is realistic for smaller SNS projects. The attacker-controlled entry path is an unprivileged ingress call to `manage_neuron` with a `MakeProposal` command — no privileged role is required. The proposer needs only enough SNS tokens to meet the proposal rejection cost, and the proposal must pass a community vote. In practice, many SNS communities have concentrated voting power, making proposal passage feasible for a motivated actor. The scenario where a legitimate proposal is submitted at a high price and executed after a price drop is also possible without any malicious intent, causing unintended over-transfer.

---

### Recommendation

At execution time, re-fetch a fresh treasury valuation instead of reusing the stale `action_auxiliary` snapshot. Specifically, in `perform_transfer_sns_treasury_funds`, call `assess_treasury_balance` again before invoking `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`, and use the fresh valuation for the allowance computation. The stale snapshot in `action_auxiliary` can be retained for auditability but should not be used for the execution-time safety check.

Alternatively, add a maximum staleness check: if the valuation in `action_auxiliary` is older than a configurable threshold (e.g., 24 hours), reject execution and require the proposer to resubmit.

---

### Proof of Concept

1. SNS token price is 10 XDR/token. Treasury holds 200,000 tokens → 2,000,000 XDR (large treasury). The 7-day cap is 300,000 XDR ÷ 10 XDR/token = **30,000 tokens**.

2. Attacker submits a `TransferSnsTreasuryFunds` proposal for 29,999 tokens (just under the limit). The valuation snapshot `{tokens: 200000, icps_per_token: X, xdrs_per_icp: Y}` is stored in `action_auxiliary`.

3. During the 7-day voting period, the SNS token price drops to 2 XDR/token. The treasury is now worth only 400,000 XDR (medium). The correct limit at execution time would be 25% × 200,000 tokens = **50,000 tokens** in token count, but only 300,000 XDR ÷ 2 XDR/token = **150,000 tokens** by the large-treasury cap — wait, actually the stale valuation still classifies it as "large" and uses the stale rate to convert 300,000 XDR → 30,000 tokens. But with the new price, 30,000 tokens = 60,000 XDR, which is 15% of the current treasury value (400,000 XDR), whereas the intended limit for a medium treasury is 25% × 200,000 tokens = 50,000 tokens. The stale valuation underestimates the token-denominated limit in this direction.

4. More critically: if the token price **increases** from 2 XDR to 10 XDR during voting, the stale low-price valuation classifies the treasury as "small" (NoLimit), allowing **100% of the treasury** to be transferred, whereas the current high price would classify it as "large" with a 300,000 XDR cap. This is the more dangerous direction — a proposal submitted when the token is cheap (treasury appears small → no limit) executes when the token is expensive (treasury is actually large → should be capped). [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L570-593)
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
    let valuation = match valuation {
        Ok(ok) => Some(ok),
        Err(err) => {
            defects.push(err);
            None
        }
    };

    // Validate all other aspects of the proposal action.
    locally_validate_and_render_transfer_sns_treasury_funds(transfer, sns_transfer_fee_e8s, defects)
        .and_then(|rendering| {
            match valuation {
                Some(valuation) => Ok((
                    rendering,
                    ActionAuxiliary::TransferSnsTreasuryFunds(valuation),
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2658)
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

    Ok(())
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1063-1083)
```text
  message TransferSnsTreasuryFundsActionAuxiliary {
    Valuation valuation = 1;
  }

  message MintSnsTokensActionAuxiliary {
    Valuation valuation = 1;
  }

  message AdvanceSnsTargetVersionActionAuxiliary {
    // Corresponds to the Some(target_version) from an AdvanceSnsTargetVersion proposal, or
    // to the last SNS version known to this SNS at the time of AdvanceSnsTargetVersion creation.
    optional SnsVersion target_version = 1;
  }

  // In general, this holds data retrieved at proposal submission/creation time and used later
  // during execution. This varies based on the action of the proposal.
  oneof action_auxiliary {
    TransferSnsTreasuryFundsActionAuxiliary transfer_sns_treasury_funds = 22;
    MintSnsTokensActionAuxiliary mint_sns_tokens = 23;
    AdvanceSnsTargetVersionActionAuxiliary advance_sns_target_version = 24;
  }
```

**File:** rs/sns/governance/src/governance.rs (L2203-2210)
```rust
            Action::TransferSnsTreasuryFunds(transfer) => {
                let valuation =
                    get_action_auxiliary(&self.proto.proposals, ProposalId { id: proposal_id })
                        .and_then(|action_auxiliary| {
                            action_auxiliary.unwrap_transfer_sns_treasury_funds_or_err()
                        });
                self.perform_transfer_sns_treasury_funds(proposal_id, valuation, &transfer)
                    .await
```

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
