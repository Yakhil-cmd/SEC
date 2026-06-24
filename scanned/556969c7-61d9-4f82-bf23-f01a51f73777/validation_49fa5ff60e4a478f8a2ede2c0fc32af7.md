### Title
`MintSnsTokens` Bypasses the 7-Day Treasury Amount Limit Due to Disabled Upper-Bound Check — (`rs/sns/governance/src/proposal.rs`)

---

### Summary

The SNS governance canister enforces a 7-day rolling upper bound on the total amount of tokens that can be moved via `TransferSnsTreasuryFunds` proposals. An identical limit is defined for `MintSnsTokens` proposals but is intentionally disabled in the current production code, with the active implementation returning `Decimal::MAX` (effectively no limit). This creates a direct bypass: a governance participant with sufficient voting power who would be blocked by the 7-day cap on treasury transfers can instead use `MintSnsTokens` to mint an equivalent or larger amount of SNS tokens with no cap enforced, inflating the token supply and diluting all other holders.

---

### Finding Description

The `TokenProposalAction` trait in `rs/sns/governance/src/proposal.rs` defines `recent_amount_total_upper_bound_tokens` as the hard cap on the 7-day rolling total for a given proposal type. Both `TransferSnsTreasuryFunds` and `MintSnsTokens` implement this trait.

**`TransferSnsTreasuryFunds` — properly limited:** [1](#0-0) 

This calls `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which computes a real cap based on treasury valuation (e.g., 25% of treasury value for a medium-sized treasury, or 300,000 XDR for a large one). [2](#0-1) 

**`MintSnsTokens` — limit disabled, returns `Decimal::MAX`:** [3](#0-2) 

The correct implementation is commented out with `TODO(NNS1-2982): Uncomment`, and the active stub returns `Decimal::MAX`. Because the shared validation function `treasury_valuation_if_proposal_amount_is_small_enough_or_err` checks `proposal_amount_tokens > allowance_remainder_tokens`, and `allowance_remainder_tokens` is derived from `Decimal::MAX`, this check **always passes** for any `MintSnsTokens` proposal regardless of amount. [4](#0-3) 

**Execution-time check also absent for `MintSnsTokens`:**

`perform_transfer_sns_treasury_funds` calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` as a second guard at execution time: [5](#0-4) 

`perform_mint_sns_tokens` has no equivalent guard — it directly mints tokens with no amount check: [6](#0-5) 

The helper `total_minting_amount_tokens` that would track the 7-day minting total is also marked `#[allow(unused)]` and is dead code: [7](#0-6) 

---

### Impact Explanation

A governance participant (or coalition) with sufficient voting power to pass `MintSnsTokens` proposals can mint an **unbounded** quantity of SNS tokens within a 7-day window. The 7-day limit is a hard safety cap designed to prevent rapid token inflation even when a governance majority exists. Because `MintSnsTokens` bypasses this cap entirely, the protection is asymmetric:

- `TransferSnsTreasuryFunds` of 30% of treasury value → **blocked** by the 7-day cap.
- `MintSnsTokens` of 10× the entire treasury value → **allowed** with no cap.

The concrete impact is **ledger conservation / token supply integrity**: SNS token holders suffer unlimited dilution of their stake, and the SNS treasury's effective value is undermined, because minting new tokens is economically equivalent to draining the treasury but is not subject to the same guard.

**Severity: MEDIUM** — the impact is significant (unlimited token inflation) but requires governance voting power to trigger.

---

### Likelihood Explanation

Any SNS with a concentrated token distribution (e.g., a single whale neuron holding majority voting power, or a small coalition) can exploit this without needing to compromise any key or bypass any cryptographic primitive. The SNS governance system is explicitly designed to allow any neuron holder to submit proposals; the 7-day limit is the only protection against rapid large-scale token movements. Since that limit is disabled for `MintSnsTokens`, the attack surface is open to any actor who can pass governance proposals.

**Likelihood: MEDIUM** — requires governance voting power, but many SNS DAOs have concentrated initial distributions.

---

### Recommendation

Uncomment the proper `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (as indicated by the `TODO(NNS1-2982): Uncomment` comment) and delete the `Decimal::MAX` stub:

```rust
// In impl TokenProposalAction for MintSnsTokens:
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {:?}", treasury_limit_error)
        })
}
```

Additionally, add an execution-time check in `perform_mint_sns_tokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`, and activate `total_minting_amount_tokens` so the 7-day rolling total for minting is tracked and enforced.

---

### Proof of Concept

1. Deploy an SNS with a whale neuron holding majority voting power.
2. Observe that a `TransferSnsTreasuryFunds` proposal for 30% of treasury value is blocked at submission time with "Amount is too large."
3. Submit a `MintSnsTokens` proposal for 10× the treasury value. The validation in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes `allowance_remainder_tokens = Decimal::MAX - 0 = Decimal::MAX`, so `proposal_amount_tokens > allowance_remainder_tokens` is always `false`. The proposal is accepted.
4. Vote to pass the proposal with the whale neuron. `perform_mint_sns_tokens` executes with no amount check, minting the full requested amount directly via `self.ledger.transfer_funds`.
5. All other token holders' stakes are diluted by the newly minted supply, with no 7-day cap enforced. [8](#0-7) [6](#0-5)

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

**File:** rs/sns/governance/src/proposal.rs (L863-869)
```rust
    fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
        transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(*valuation)
            // Err is most likely a bug.
            .map_err(|treasury_limit_error| {
                format!("Unable to validate amount: {treasury_limit_error:?}",)
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

**File:** rs/sns/governance/src/proposal.rs (L2705-2728)
```rust
/// Analogous to total_treasury_transfer_amount_tokens. Of course, this considers MintSnsTokens
/// proposals instead of TransferSnsTreasuryFunds proposals.
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L8-18)
```rust
pub fn transfer_sns_treasury_funds_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}

pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
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
