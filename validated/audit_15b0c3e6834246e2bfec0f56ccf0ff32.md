### Title
`MintSnsTokens` 7-Day Minting Cap Bypassed via `Decimal::MAX` Upper Bound - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The `MintSnsTokens` implementation of `TokenProposalAction::recent_amount_total_upper_bound_tokens` unconditionally returns `Decimal::MAX` instead of the actual treasury-valuation-based limit. This renders the 7-day SNS token minting cap completely ineffective, allowing unlimited token minting via governance proposals with no upper bound enforced.

---

### Finding Description

In `rs/sns/governance/src/proposal.rs`, the `TokenProposalAction` trait governs both `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals. The trait method `recent_amount_total_upper_bound_tokens` is supposed to return the maximum tokens allowed to be minted/transferred within a 7-day rolling window, derived from the treasury valuation.

For `TransferSnsTreasuryFunds`, the implementation correctly enforces the limit:

```rust
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(*valuation)
        .map_err(|treasury_limit_error| { ... })
}
```

For `MintSnsTokens`, the real implementation is commented out and replaced with a stub that returns `Decimal::MAX`:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/

// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [1](#0-0) 

The limit check in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` then becomes:

```rust
let allowance_remainder_tokens = max_tokens.checked_sub(spent_tokens)...;
// max_tokens = Decimal::MAX → allowance_remainder_tokens ≈ Decimal::MAX
if proposal_amount_tokens > allowance_remainder_tokens {
    // This condition is NEVER true
}
``` [2](#0-1) 

Additionally, unlike `TransferSnsTreasuryFunds` which has a second execution-time check (`transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`), `perform_mint_sns_tokens` performs no limit check at execution time: [3](#0-2) 

The `total_minting_amount_tokens` function (which correctly computes the 7-day rolling total) is even marked `#[allow(unused)]`, confirming it is not enforced: [4](#0-3) 

The `ProposalsAmountTotalUpperBound` library correctly defines the intended limit (up to 25% of treasury for medium-sized treasuries, capped at 300,000 XDR for large ones), but it is never applied to `MintSnsTokens`: [5](#0-4) 

---

### Impact Explanation

Any SNS governance majority can pass `MintSnsTokens` proposals of arbitrary size — far exceeding the intended treasury-based 7-day cap — minting unlimited SNS tokens. This directly breaks SNS tokenomics, causes unbounded inflation, and dilutes existing token holders. The minted tokens are immediately credited to the target account on the SNS ledger with no recourse.

---

### Likelihood Explanation

The entry path is a standard SNS governance ingress call (`manage_neuron` → `MakeProposal` → `MintSnsTokens`). Any neuron holder who can achieve or coordinate a governance majority can exploit this. The SNS governance system is open to all neuron holders, making this reachable by any sufficiently powerful participant. The bug is present in production code today with no runtime mitigation.

---

### Recommendation

Uncomment the proper `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (as indicated by the `TODO(NNS1-2982): Uncomment` comment), which calls `mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)`. Additionally, add an execution-time limit check for `MintSnsTokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` to enforce the cap at both proposal submission and execution. [6](#0-5) 

---

### Proof of Concept

1. Deploy or use an existing SNS with a treasury of any size.
2. Achieve governance majority (e.g., hold or coordinate sufficient neuron voting power).
3. Submit a `MintSnsTokens` proposal with `amount_e8s` set to any value exceeding the treasury-based 7-day limit (e.g., 100× the treasury balance in tokens).
4. The proposal passes `validate_and_render_mint_sns_tokens` → `treasury_valuation_if_proposal_amount_is_small_enough_or_err` without rejection, because `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`.
5. Upon execution, `perform_mint_sns_tokens` mints the full requested amount with no cap check.
6. Repeat in the same 7-day window to mint further unlimited amounts — `total_minting_amount_tokens` correctly accumulates the total, but the upper bound is never compared against it. [7](#0-6)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L799-814)
```rust
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
```

**File:** rs/sns/governance/src/proposal.rs (L875-930)
```rust
async fn validate_and_render_mint_sns_tokens(
    mint_sns_tokens: &MintSnsTokens,
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

    // Validate amount. (This requires calling CMC and the swap canister; hence, await.)
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        mint_sns_tokens,
    )
    .await;
    let valuation = match valuation {
        Ok(ok) => Some(ok),
        Err(err) => {
            defects.push(err);
            None
        }
    };

    locally_validate_and_render_mint_sns_tokens(mint_sns_tokens, sns_transfer_fee_e8s, defects)
        .and_then(|rendering| {
            match valuation {
                Some(valuation) => Ok((rendering, ActionAuxiliary::MintSnsTokens(valuation))),

                // Proof that this never happens:
                //
                //   1. valuation = None means that amount_result was Err.
                //
                //   2. In that case, nonempty defects was passed to
                //      locally_validate_and_render_mint_sns_tokens.
                //
                //   3. In that case, the function always returns Err.
                //
                //   4. Then, this closure doesn't get called.
                None => Err(
                    "There is a bug in the amount validator. Somehow, no valuation, \
                     even though a rendering was generated."
                        .to_string(),
                ),
            }
        })
}
```

**File:** rs/sns/governance/src/proposal.rs (L1025-1041)
```rust
    /* TODO(NNS1-2982): Uncomment.
    fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
        mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
            // Err is most likely a bug.
            .map_err(|treasury_limit_error| {
                format!("Unable to validate amount: {:?}", treasury_limit_error,)
            })
    }
    */

    // TODO(NNS1-2982): Delete.
    fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
        // Ideally, we'd return infinity, but Decimal does not have that. This is the next best
        // thing, and should be good enough, because we have already planned the obselences of this
        // code (see tickets NNS1-298(1|2)).
        Ok(Decimal::MAX)
    }
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

**File:** rs/sns/governance/src/proposal.rs (L2707-2728)
```rust
#[allow(unused)] // TODO(NNS1-2910): Delete this.
fn total_minting_amount_tokens<'a>(
    proposals: impl Iterator<Item = &'a ProposalData>,
    min_executed_timestamp_seconds: u64,
) -> Result<Decimal, String> {
    let filter_proposal_action_amount_e8s = |action: &Action| {
        let mint = match action {
            Action::MintSnsTokens(ok) => ok,
            // Skip other types of proposals.
            _ => return None,
        };

        mint.amount_e8s
    };

    total_proposal_amounts_tokens(
        proposals,
        "MintSnsTokens",
        filter_proposal_action_amount_e8s,
        min_executed_timestamp_seconds,
    )
}
```

**File:** rs/sns/governance/src/governance.rs (L3062-3088)
```rust
    async fn perform_mint_sns_tokens(
        &mut self,
        mint: MintSnsTokens,
    ) -> Result<(), GovernanceError> {
        let to = Account {
            owner: mint
                .to_principal
                .ok_or(GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    "Expected mint to have a target principal",
                ))?
                .0,
            subaccount: mint
                .to_subaccount
                .as_ref()
                .map(|s| bytes_to_subaccount(&s.subaccount[..]))
                .transpose()?,
        };
        let amount_e8s = mint.amount_e8s.ok_or(GovernanceError::new_with_message(
            ErrorType::InvalidProposal,
            "Expected MintSnsTokens to have an an amount_e8s",
        ))?;
        self.ledger
            .transfer_funds(amount_e8s, 0, None, to, mint.memo())
            .await?;
        Ok(())
    }
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
