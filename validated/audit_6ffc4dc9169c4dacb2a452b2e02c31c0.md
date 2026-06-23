### Title
Disabled `MintSnsTokens` 7-Day Rate-Limit Allows Unbounded SNS Token Inflation via Governance Proposals - (File: rs/sns/governance/src/proposal.rs)

### Summary
The `MintSnsTokens` SNS governance proposal action has its 7-day minting upper-bound enforcement explicitly disabled. The `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` unconditionally returns `Decimal::MAX` instead of calling the real limit function, meaning any SNS governance majority can mint an unlimited number of SNS tokens in any 7-day window with no rate-limiting protection. The analogous `TransferSnsTreasuryFunds` action has this protection properly enforced.

### Finding Description

The SNS governance system defines a `TokenProposalAction` trait with a method `recent_amount_total_upper_bound_tokens` that is supposed to cap the total tokens that can be minted or transferred within a 7-day rolling window. For `TransferSnsTreasuryFunds`, this is correctly wired to `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which enforces tiered limits based on treasury valuation (100% for small, 25% for medium, capped at 300,000 XDR for large treasuries).

For `MintSnsTokens`, however, the real enforcement is commented out and replaced with a stub that returns `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
```

The validation function `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes:

```rust
let max_tokens = MyTokenProposalAction::recent_amount_total_upper_bound_tokens(&valuation)?;
let allowance_remainder_tokens = max_tokens.checked_sub(spent_tokens)...;
if proposal_amount_tokens > allowance_remainder_tokens { return Err(...) }
```

Since `max_tokens = Decimal::MAX`, `allowance_remainder_tokens` is effectively unbounded, and `proposal_amount_tokens` (derived from a `u64` field `amount_e8s`) can never exceed it. The guard never fires.

This is confirmed by the integration test in `rs/sns/integration_tests/src/sns_treasury.rs`, where the second `MintSnsTokens` proposal is expected to be rejected (the assertion is commented out with `TODO(NNS1-2982)`) but currently succeeds:

```rust
doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
``` [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

Any SNS governance majority can pass repeated `MintSnsTokens` proposals minting arbitrary amounts of SNS tokens in any 7-day window, with no rate-limiting protection. This directly inflates the SNS token supply without bound, devaluing existing token holders' stakes. The `TransferSnsTreasuryFunds` action enforces the same limit correctly, so the asymmetry is a clear implementation gap. The minted tokens are credited to an arbitrary principal via the SNS ledger's minting account path. [4](#0-3) [5](#0-4) 

### Likelihood Explanation

The entry path is an SNS governance proposal submitted by any token holder with sufficient voting power. No privileged key or admin access beyond normal SNS governance participation is required. The disabled check is in production code (not a test or config file). Any SNS with a concentrated token distribution (e.g., a whale holding majority voting power, or a coordinated group) can exploit this immediately. The `TODO(NNS1-2982)` comments confirm this is a known, tracked gap that has not yet been closed. [6](#0-5) [7](#0-6) 

### Recommendation

Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` and delete the `Decimal::MAX` stub:

```rust
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {:?}", treasury_limit_error)
        })
}
```

Also uncomment the corresponding assertion in the integration test `sns_can_mint_funds_via_proposals` and remove the `doomed_make_proposal_result.unwrap()` bypass line. Additionally, add an execution-time re-check analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` for `MintSnsTokens`. [8](#0-7) 

### Proof of Concept

1. Deploy an SNS with a token distribution giving a single principal majority voting power.
2. Submit a `MintSnsTokens` proposal for `u64::MAX` e8s (≈ 184 billion tokens) to an arbitrary account.
3. Vote to adopt with the majority neuron.
4. Observe the proposal executes successfully — `perform_mint_sns_tokens` calls `ledger.transfer_funds(amount_e8s, 0, None, to, memo)` with no supply cap check.
5. Repeat immediately: submit another proposal for `u64::MAX` e8s. It passes validation again because `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`.
6. The SNS token total supply grows without bound across successive proposals within the same 7-day window, with no enforcement of the intended rate limit. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-53)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
```

**File:** rs/sns/governance/src/proposal.rs (L793-814)
```rust
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

**File:** rs/sns/integration_tests/src/sns_treasury.rs (L942-966)
```rust
    /* TODO(NNS1-2982): Uncomment.
    let err = doomed_make_proposal_result.unwrap_err();
    let SnsGovernanceError {
        error_type,
        error_message,
    } = &err;
    assert_eq!(
        SnsErrorType::try_from(*error_type),
        Ok(SnsErrorType::InvalidProposal),
        "{:#?}",
        err,
    );
    let error_message = error_message.to_lowercase();
    for snip in [
        "amount",
        "too large",
        "2222",
        "upper bound",
        "exceeded",
        "try again",
    ] {
        assert!(error_message.contains(snip), "{:#?}", err);
    }
    */
    doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
