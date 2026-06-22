### Title
`MintSnsTokens` Proposal Bypasses Valuation-Based Minting Limit, Allowing Unbounded SNS Token Inflation - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The `MintSnsTokens` SNS governance proposal action bypasses the valuation-based 7-day minting limit that is meant to protect against excessive token issuance. While `TransferSnsTreasuryFunds` enforces a treasury-valuation-derived cap at both proposal submission and execution time, `MintSnsTokens` hardcodes `Decimal::MAX` as the upper bound (effectively no limit) and has no execution-time valuation check at all. This is directly analogous to the reported vulnerability: an asset operation proceeds without the required valuation/oracle sanity check, because the check is structurally bypassed.

---

### Finding Description

**Vulnerability class**: Ledger conservation bug / governance authorization bypass — valuation check skipped for a token-issuance action.

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to enforce a 7-day rolling cap on treasury-affecting proposals, derived from the live treasury valuation. For `TransferSnsTreasuryFunds`, this is correctly implemented: [1](#0-0) 

For `MintSnsTokens`, the implementation is intentionally disabled and replaced with a stub that returns `Decimal::MAX`: [2](#0-1) 

The shared validation path `treasury_valuation_if_proposal_amount_is_small_enough_or_err` fetches the live treasury valuation and computes `max_tokens` from it — but since `MintSnsTokens::recent_amount_total_upper_bound_tokens` always returns `Decimal::MAX`, the comparison `proposal_amount_tokens > allowance_remainder_tokens` is always false, and any amount passes: [3](#0-2) 

At execution time, `perform_transfer_sns_treasury_funds` calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` to re-validate the amount against the stored valuation: [4](#0-3) 

But `perform_mint_sns_tokens` has **no such check** — it directly mints the requested amount with zero valuation enforcement: [5](#0-4) 

The valuation is fetched, stored in `ActionAuxiliary::MintSnsTokens(valuation)`, and then completely ignored at execution: [6](#0-5) 

---

### Impact Explanation

An SNS governance proposal can mint an arbitrarily large quantity of SNS tokens — up to `u64::MAX` e8s per proposal — with no valuation-based guardrail at either submission or execution time. This enables:

- **Unbounded token inflation**: the SNS token supply can be inflated without limit, destroying value for all existing holders.
- **Incorrect treasury accounting**: the minting limit that is supposed to be proportional to treasury size (as computed by `mint_sns_tokens_7_day_total_upper_bound_tokens`) is never enforced, making the 7-day rolling window meaningless for minting.
- **Asymmetric protection**: `TransferSnsTreasuryFunds` (moving existing tokens) is protected; `MintSnsTokens` (creating new tokens from nothing) is not — the more dangerous operation has weaker controls. [7](#0-6) 

---

### Likelihood Explanation

Exploiting this requires passing a `MintSnsTokens` governance proposal, which requires a voting majority in the SNS. This is not trivially achievable by an anonymous attacker. However:

- SNS DAOs with low token distribution, low participation, or concentrated voting power are directly at risk from a malicious large token holder.
- Even without malicious intent, the absence of the guardrail means legitimate governance participants cannot rely on the system to prevent accidental over-minting.
- The attacker entry point is the standard `manage_neuron` / proposal submission flow — a fully valid, unprivileged product flow.

---

### Recommendation

1. Uncomment the correct implementation of `recent_amount_total_upper_bound_tokens` for `MintSnsTokens` (as indicated by the `TODO(NNS1-2982): Uncomment` comment at line 1025): [8](#0-7) 

2. Add an execution-time valuation check in `perform_mint_sns_tokens`, analogous to the check in `perform_transfer_sns_treasury_funds`, using the valuation stored in `ActionAuxiliary::MintSnsTokens`.

3. Retrieve and use the stored `MintSnsTokensActionAuxiliary` valuation at execution time, mirroring the pattern used for `TransferSnsTreasuryFunds`: [9](#0-8) 

---

### Proof of Concept

1. An SNS token holder with sufficient voting power submits a `MintSnsTokens` proposal with `amount_e8s = u64::MAX` (≈ 1.84 × 10¹⁰ tokens).
2. During proposal validation, `treasury_valuation_if_proposal_amount_is_small_enough_or_err` is called. The live treasury valuation is fetched correctly, but `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, so `proposal_amount_tokens > allowance_remainder_tokens` is always false — the proposal passes validation unconditionally.
3. The proposal is voted on and adopted.
4. At execution, `perform_mint_sns_tokens` calls `self.ledger.transfer_funds(amount_e8s, 0, None, to, mint.memo())` with `amount_e8s = u64::MAX` and no valuation check.
5. The SNS ledger mints `u64::MAX` e8s of SNS tokens to the attacker's account, inflating the total supply by orders of magnitude and collapsing the token's value. [10](#0-9)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L792-814)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L2203-2212)
```rust
            Action::TransferSnsTreasuryFunds(transfer) => {
                let valuation =
                    get_action_auxiliary(&self.proto.proposals, ProposalId { id: proposal_id })
                        .and_then(|action_auxiliary| {
                            action_auxiliary.unwrap_transfer_sns_treasury_funds_or_err()
                        });
                self.perform_transfer_sns_treasury_funds(proposal_id, valuation, &transfer)
                    .await
            }
            Action::MintSnsTokens(mint) => self.perform_mint_sns_tokens(mint).await,
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
