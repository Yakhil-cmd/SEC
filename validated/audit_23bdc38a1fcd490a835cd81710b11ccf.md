### Title
Stale Treasury Valuation Snapshot Used as Execution-Time Limit for `MintSnsTokens` Proposals — (`File: rs/sns/governance/src/governance.rs`, `rs/sns/governance/src/proposal.rs`)

### Summary

The SNS governance canister snapshots the treasury valuation at proposal-submission time and stores it in `ProposalData::action_auxiliary` as a `MintSnsTokensActionAuxiliary { valuation }`. For `TransferSnsTreasuryFunds`, this stale valuation is re-used at execution time to enforce the 7-day spending cap. For `MintSnsTokens`, the execution path skips the valuation check entirely — `perform_mint_sns_tokens` does not retrieve the stored valuation and performs no execution-time limit check — while the submission-time upper bound is permanently disabled (`Decimal::MAX`). The net result is that any amount of SNS tokens can be minted via a passed proposal regardless of treasury size, bypassing the intended economic safeguard.

### Finding Description

**Submission-time path (correct):**

`validate_and_render_mint_sns_tokens` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which fetches a live valuation from the swap canister and CMC, computes the 7-day upper bound, and stores the result in `ActionAuxiliary::MintSnsTokens(valuation)`.

However, the `TokenProposalAction` implementation for `MintSnsTokens` permanently overrides the upper-bound function to return `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
```

This means the submission-time check always passes for any amount.

**Execution-time path (missing check):**

`perform_action` dispatches `Action::MintSnsTokens(mint)` directly to `perform_mint_sns_tokens(mint)` with no valuation lookup and no spending-cap enforcement:

```rust
Action::MintSnsTokens(mint) => self.perform_mint_sns_tokens(mint).await,
```

`perform_mint_sns_tokens` simply mints the requested amount unconditionally:

```rust
self.ledger.transfer_funds(amount_e8s, 0, None, to, mint.memo()).await?;
```

Compare this to `TransferSnsTreasuryFunds`, which retrieves the stored valuation and calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` before executing the transfer.

The stale-valuation analog to the reported bug is: the valuation snapshot taken at proposal creation time is never used to enforce any limit at execution time for `MintSnsTokens`. The `TransferSnsTreasuryFunds` path at least re-uses the stale valuation as a cap at execution time; `MintSnsTokens` has no cap at all at either time.

### Impact Explanation

An SNS community (or a whale neuron holder with sufficient voting power) can pass a `MintSnsTokens` proposal for an arbitrarily large amount — e.g., minting tokens equal to the entire circulating supply — and the proposal will execute without any treasury-size-based limit check. This allows:

- Unlimited dilution of existing token holders.
- Bypassing the intended 7-day, treasury-proportional spending cap that was designed to limit the rate at which an SNS can redistribute its treasury.
- If the SNS token is listed on exchanges, this can be used to dump a massive newly minted supply, crashing the price.

The impact is a **ledger conservation / governance authorization bug**: the economic safeguard (the 7-day cap proportional to treasury value) is completely absent for `MintSnsTokens` at execution time, and the submission-time check is also disabled via `Decimal::MAX`.

### Likelihood Explanation

Any SNS neuron holder with enough voting power to pass a `MintSnsTokens` proposal can trigger this. This is an unprivileged governance participant — no admin key or threshold corruption is required. The attacker only needs to control enough voting power to adopt the proposal (3% of total + 50% of exercised for normal proposals, or 20% + 67% for critical ones). In many SNS deployments, a single whale neuron or a small coalition can meet this threshold. The code path is reachable via the standard `manage_neuron` → `make_proposal` → proposal adoption → `perform_action` flow.

### Recommendation

1. **Re-enable the execution-time cap for `MintSnsTokens`**: In `perform_action`, retrieve the stored `MintSnsTokensActionAuxiliary` valuation (analogous to how `TransferSnsTreasuryFunds` does it) and call an equivalent of `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` before minting.

2. **Uncomment the real upper-bound implementation**: The commented-out `recent_amount_total_upper_bound_tokens` for `MintSnsTokens` (gated behind `TODO(NNS1-2982)`) should be activated so that the submission-time check also enforces the treasury-proportional cap.

3. **Add a `mint_sns_tokens_amount_is_small_enough_at_execution_time_or_err` function** mirroring the existing `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`.

### Proof of Concept

**Step 1:** An SNS with a treasury of 1,000,000 SNS tokens (worth ~10,000,000 XDR) is deployed. The intended 7-day cap would be 25% = 250,000 tokens.

**Step 2:** A whale neuron holder submits a `MintSnsTokens` proposal for 10,000,000 tokens (10× the entire treasury). At submission time, `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, so the check passes unconditionally.

**Step 3:** The proposal is adopted (whale has majority voting power).

**Step 4:** `perform_action` dispatches to `perform_mint_sns_tokens`, which calls `self.ledger.transfer_funds(10_000_000 * E8, 0, None, to, memo)` with no cap check. 10,000,000 new tokens are minted to the attacker's account.

**Relevant code locations:**

- Disabled upper bound: [1](#0-0) 
- Missing execution-time check in dispatch: [2](#0-1) 
- `perform_mint_sns_tokens` with no limit enforcement: [3](#0-2) 
- Contrast with `TransferSnsTreasuryFunds` which does retrieve valuation and check: [4](#0-3) 
- Execution-time check that exists for `TransferSnsTreasuryFunds` but not `MintSnsTokens`: [5](#0-4) 
- The commented-out real implementation that should be active: [6](#0-5)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L1025-1033)
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
```

**File:** rs/sns/governance/src/proposal.rs (L1035-1041)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L2212-2212)
```rust
            Action::MintSnsTokens(mint) => self.perform_mint_sns_tokens(mint).await,
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
