### Title
`MintSnsTokens` 7-Day Minting Ceiling Permanently Disabled — Unlimited SNS Token Minting via Governance Proposals - (File: `rs/sns/governance/src/proposal.rs`)

### Summary

The SNS governance canister intentionally implements a 7-day rolling minting ceiling for `MintSnsTokens` proposals (analogous to the `mintCeiling` in the Alchemix report). However, the enforcement of that ceiling is **permanently commented out** in production code, and the active implementation returns `Decimal::MAX` as the upper bound. This means any SNS community with sufficient voting power can pass an unlimited number of `MintSnsTokens` proposals in any 7-day window, bypassing the intended treasury-protection limit entirely.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens()` to cap how many SNS tokens can be minted within a 7-day window. For `TransferSnsTreasuryFunds`, this ceiling is correctly enforced. For `MintSnsTokens`, the real ceiling implementation is commented out under `TODO(NNS1-2982)`, and the active stub unconditionally returns `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    // Ideally, we'd return infinity, but Decimal does not have that. This is the next best
    // thing, and should be good enough, because we have already planned the obselences of this
    // code (see tickets NNS1-298(1|2)).
    Ok(Decimal::MAX)
}
```

The real implementation — which calls `mint_sns_tokens_7_day_total_upper_bound_tokens()` — is blocked behind a comment:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
    ...
}
*/
```

The validation path in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes `max_tokens` from `recent_amount_total_upper_bound_tokens`, then checks `proposal_amount_tokens > allowance_remainder_tokens`. Since `max_tokens = Decimal::MAX`, the check never triggers. The integration test `sns_can_mint_funds_via_proposals` explicitly confirms this: the second mint proposal that should be rejected is instead allowed to succeed, with the assertion commented out and replaced by `doomed_make_proposal_result.unwrap()`.

The `can_be_purged` logic correctly retains executed `MintSnsTokens` proposals for 7 days (so the accounting data is present), and `total_minting_amount_tokens` correctly sums recent mints — but the sum is compared against `Decimal::MAX`, making the guard a no-op.

### Impact Explanation

Any SNS with a sufficiently large neuron (or colluding majority) can pass back-to-back `MintSnsTokens` proposals with no 7-day cap. This allows unlimited inflation of the SNS token supply within any time window, draining value from all token holders. The intended protection — capping minting at 25% of treasury value per 7 days for medium-sized treasuries — is completely absent for `MintSnsTokens` while it is correctly enforced for `TransferSnsTreasuryFunds`. The asymmetry means minting is a privileged bypass path for the treasury limit.

### Likelihood Explanation

The entry path is a standard SNS governance proposal (`MintSnsTokens` action), submittable by any principal holding a neuron with sufficient stake. No special privilege beyond normal SNS voting majority is required. The code is deployed on mainnet SNS instances. The bug is confirmed by the integration test which explicitly documents that the second mint succeeds when it should fail.

### Recommendation

Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (tracked as `TODO(NNS1-2982)`) and delete the `Decimal::MAX` stub. Also uncomment the corresponding assertion in the integration test `sns_can_mint_funds_via_proposals`. Additionally, add an execution-time check analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` for `MintSnsTokens` proposals to guard against race conditions between proposal submission and execution.

### Proof of Concept

1. Deploy an SNS with a treasury of 50,000 SNS tokens (medium size, ~500,000 XDR). The intended 7-day cap is 25% = 12,500 tokens.
2. Submit a `MintSnsTokens` proposal for 12,500 tokens. It passes and executes.
3. Immediately submit another `MintSnsTokens` proposal for 12,500 tokens. Under the intended limit, this should be rejected at submission time. Instead, `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, so `allowance_remainder_tokens = Decimal::MAX - 12,500 ≈ Decimal::MAX`, and the check `12,500 > Decimal::MAX` is false. The proposal is accepted and executes.
4. Repeat indefinitely within the same 7-day window, minting unlimited tokens.

The integration test at `rs/sns/integration_tests/src/sns_treasury.rs:966` confirms step 3 with `doomed_make_proposal_result.unwrap()` — the proposal that should fail instead succeeds. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-53)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
```

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

**File:** rs/sns/governance/src/proposal.rs (L2437-2469)
```rust
    pub(crate) fn can_be_purged(&self, now_seconds: u64) -> bool {
        // Retain proposals that have not gone through the full lifecycle.
        if !self.status().is_final() {
            return false;
        }
        if !self.reward_status(now_seconds).is_final() {
            return false;
        }

        // At this point, we can let go of most proposals. The only special case is
        // TransferSnsTreasuryFunds and MintSnsTokens (the common thread between these is that these
        // affect the value of the treasury). We want to hang onto those for at least 7 days after
        // they have been successfully executed. This is because they are still needed for the
        // purposes of limiting amounts.
        let Some(proposal) = &self.proposal else {
            log!(ERROR, "Proposal {:?} missing `proposal` field", self.id);
            return true;
        };
        let retention_duration_seconds = match &proposal.action {
            Some(Action::TransferSnsTreasuryFunds(_)) => {
                EXECUTED_TRANSFER_SNS_TREASURY_FUNDS_PROPOSAL_RETENTION_DURATION_SECONDS
            }
            Some(Action::MintSnsTokens(_)) => {
                EXECUTED_MINT_SNS_TOKENS_PROPOSAL_RETENTION_DURATION_SECONDS
            }
            _ => return true,
        };

        // Only hang onto proposals that were executed recently enough. In other words, let older
        // proposals age out.
        let earliest_unpurgeable_executed_timestamp_seconds =
            now_seconds - retention_duration_seconds;
        self.executed_timestamp_seconds < earliest_unpurgeable_executed_timestamp_seconds
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
