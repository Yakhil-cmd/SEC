### Title
Stale Treasury Valuation at Execution Time Allows 7-Day XDR Transfer Limit to Be Exceeded - (`rs/sns/governance/src/proposal.rs`)

### Summary

The SNS governance `TransferSnsTreasuryFunds` execution path uses a treasury `Valuation` captured at **proposal submission time** to enforce the 7-day transfer limit at **execution time**. When token prices rise significantly between submission and execution, the stale valuation overstates the token-denominated allowance, allowing the protocol's 300,000 XDR hard cap to be exceeded by a large multiple.

### Finding Description

When a `TransferSnsTreasuryFunds` proposal is submitted, `validate_and_render_transfer_sns_treasury_funds` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which fetches the live treasury balance and ICP/XDR price and stores the result as `ActionAuxiliary::TransferSnsTreasuryFunds(valuation)` inside `ProposalData.action_auxiliary`. [1](#0-0) 

This `Valuation` snapshot — containing `tokens`, `icps_per_token`, and `xdrs_per_icp` at submission time — is persisted in the proposal record. [2](#0-1) 

At execution time, `perform_transfer_sns_treasury_funds` retrieves this stored valuation and passes it directly to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`: [3](#0-2) 

That function computes `allowance_tokens` from the stale valuation: [4](#0-3) 

The allowance computation in `ProposalsAmountTotalUpperBound::in_tokens` converts the 300,000 XDR hard cap into a token count using the **submission-time** `xdrs_per_token`: [5](#0-4) 

The error message in the execution-time check even acknowledges the staleness explicitly: [6](#0-5) 

### Impact Explanation

The 300,000 XDR cap is designed to limit the maximum value that can leave the treasury in a 7-day window. Because the cap is converted to a token count using the submission-time price, a price increase between submission and execution inflates the token allowance proportionally.

**Concrete example:**
- Submission: 1,000,000 SNS tokens, 2 ICP/token, 1 XDR/ICP → treasury = 2,000,000 XDR (large regime). Allowance = 300,000 XDR ÷ 2 XDR/token = **150,000 tokens**.
- Execution (ICP price rises to 10 XDR/ICP): 150,000 tokens × 2 ICP/token × 10 XDR/ICP = **3,000,000 XDR** transferred — 10× the intended cap.

The community voted on "150,000 tokens" without knowing the XDR value would be 10× higher at execution. The safety mechanism that was supposed to bound the XDR outflow is entirely bypassed.

### Likelihood Explanation

An SNS governance participant (neuron holder) with sufficient voting power can submit a `TransferSnsTreasuryFunds` proposal during a period of low token prices, have it pass governance vote, and benefit from a price increase before execution. SNS proposals have voting periods of days, during which token prices can move substantially. This is reachable by any unprivileged ingress sender who holds SNS neurons, without requiring any privileged access, admin keys, or majority corruption.

### Recommendation

At execution time, re-fetch the live treasury valuation (current balance and current ICP/XDR price) rather than relying on the submission-time snapshot. The execution-time check should enforce the XDR limit using current market data, analogous to how the submission-time check works in `treasury_valuation_if_proposal_amount_is_small_enough_or_err`. [7](#0-6) 

### Proof of Concept

1. SNS treasury holds 1,000,000 SNS tokens. ICP price = 1 XDR/ICP, SNS token = 2 ICP. Treasury = 2,000,000 XDR (large regime).
2. Attacker submits `TransferSnsTreasuryFunds` for 150,000 tokens. Submission-time check: 150,000 × 2 × 1 = 300,000 XDR ≤ cap. Proposal accepted; valuation snapshot `{tokens: 1_000_000, icps_per_token: 2, xdrs_per_icp: 1}` stored.
3. Governance vote passes (community sees "150,000 tokens" — a reasonable amount).
4. ICP price rises to 10 XDR/ICP before execution.
5. `perform_transfer_sns_treasury_funds` is called. It retrieves the stored valuation and calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(transfer, valuation_from_step_2, ...)`.
6. `allowance_tokens` = 300,000 XDR ÷ (2 ICP/token × 1 XDR/ICP) = 150,000 tokens. `spent_tokens` = 0. Remainder = 150,000 tokens ≥ 150,000 tokens requested. **Check passes.**
7. 150,000 tokens are transferred. At current price: 150,000 × 2 × 10 = **3,000,000 XDR** leaves the treasury — 10× the intended 300,000 XDR limit. [8](#0-7) [9](#0-8)

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

**File:** rs/sns/governance/src/proposal.rs (L770-816)
```rust
async fn treasury_valuation_if_proposal_amount_is_small_enough_or_err<MyTokenProposalAction>(
    env: &dyn Environment,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
    action: &MyTokenProposalAction,
) -> Result<Valuation, String>
where
    MyTokenProposalAction: TokenProposalAction,
{
    let spent_tokens = action.recent_amount_total_tokens(proposals, env.now())?;

    // Get valuation of the tokens in the treasury.
    let token = action.token()?;
    let valuation = assess_treasury_balance(
        token,
        env.canister_id(),
        sns_ledger_canister_id,
        swap_canister_id,
    )
    .await?;

    // From valuation, determine limit on the total from the past 7 days.
    let max_tokens = MyTokenProposalAction::recent_amount_total_upper_bound_tokens(&valuation)
        // Err is most likely a bug.
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {treasury_limit_error:?}",)
        })?;

    // Finally, inspect the proposal's amount: it must not exceed max - spent (remainder). Or if
    // you prefer, equivalently, amount + spent must be <= max.
    let allowance_remainder_tokens = max_tokens.checked_sub(spent_tokens).ok_or_else(|| {
        format!("Arithmetic error while performing {max_tokens} - {spent_tokens}",)
    })?;
    let proposal_amount_tokens = action.proposal_amount_tokens()?;
    if proposal_amount_tokens > allowance_remainder_tokens {
        // Although it might not be obvious to the user, their proposal is invalid, and we
        // consider it to be "their fault".
        return Err(format!(
            "Amount is too large. Within the past 7 days, a total of {spent_tokens} tokens has already \
             been executed in like proposals. Whereas, at most {max_tokens} is allowed. An additional \
             {proposal_amount_tokens} tokens from this proposal would cause that upper bound to be exceeded. \
             Maybe, try again in a few days?"
        ));
    }

    Ok(valuation)
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1077-1083)
```text
  // In general, this holds data retrieved at proposal submission/creation time and used later
  // during execution. This varies based on the action of the proposal.
  oneof action_auxiliary {
    TransferSnsTreasuryFundsActionAuxiliary transfer_sns_treasury_funds = 22;
    MintSnsTokensActionAuxiliary mint_sns_tokens = 23;
    AdvanceSnsTargetVersionActionAuxiliary advance_sns_target_version = 24;
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L88-110)
```rust
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
```
