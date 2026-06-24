### Title
`MintSnsTokens` 7-Day Minting Limit Protection Disabled — Unlimited SNS Token Inflation via Governance Proposal - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The SNS governance canister implements a 7-day rolling upper-bound limit on treasury outflows to protect against runaway token dilution. For `TransferSnsTreasuryFunds`, this limit is enforced both at proposal submission and at execution time. For `MintSnsTokens`, the limit is intentionally disabled at submission time (returning `Decimal::MAX`) and the execution path has no analogous execution-time check at all. Any SNS governance majority can therefore pass a `MintSnsTokens` proposal to mint an unbounded quantity of SNS tokens to any arbitrary principal, bypassing the protection mechanism entirely.

---

### Finding Description

**At proposal submission time**, `MintSnsTokens` implements the `TokenProposalAction` trait. The `recent_amount_total_upper_bound_tokens` method — which is supposed to cap the 7-day minting total — is stubbed out to return `Decimal::MAX`: [1](#0-0) 

The real implementation is commented out behind a `TODO(NNS1-2982)`: [2](#0-1) 

This means `treasury_valuation_if_proposal_amount_is_small_enough_or_err` — which is called during proposal validation — will always pass for `MintSnsTokens` regardless of the amount, because the upper bound is `Decimal::MAX`: [3](#0-2) 

**At proposal execution time**, `TransferSnsTreasuryFunds` retrieves the stored valuation and calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` before executing the transfer: [4](#0-3) [5](#0-4) 

`MintSnsTokens`, by contrast, is dispatched directly to `perform_mint_sns_tokens` with no execution-time limit check whatsoever: [6](#0-5) 

`perform_mint_sns_tokens` simply mints the requested amount with no guard: [7](#0-6) 

The `MintSnsTokens` proposal type is fully defined and reachable: [8](#0-7) 

---

### Impact Explanation

An SNS governance majority can submit and pass a `MintSnsTokens` proposal for an arbitrarily large `amount_e8s` directed to any `to_principal`. Because the 7-day upper-bound check returns `Decimal::MAX` at submission and no check exists at execution, the SNS ledger will mint the full requested amount from the minting account (governance canister) to the target. This causes unbounded SNS token inflation, diluting all existing token holders and potentially draining the economic value of the SNS treasury and token supply.

---

### Likelihood Explanation

Any actor who accumulates or controls a governance majority of SNS neuron voting power — including a single whale, a coordinated group, or an attacker who acquires tokens — can exploit this. The `MintSnsTokens` proposal type is a standard, publicly documented SNS action. The disabled limit is not a runtime configuration but a hardcoded `Decimal::MAX` stub in production code, making it unconditionally exploitable by any passing governance vote.

---

### Recommendation

1. Uncomment and activate the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (the code already exists behind the `TODO(NNS1-2982)` comment).
2. Add an execution-time minting limit check in `perform_mint_sns_tokens`, analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` used by `TransferSnsTreasuryFunds`.
3. Remove the `Decimal::MAX` stub.

---

### Proof of Concept

1. Deploy an SNS with a governance canister.
2. Acquire or control a governance majority of neuron voting power.
3. Submit a `MintSnsTokens` proposal with `amount_e8s = u64::MAX` and `to_principal = attacker_principal`.
4. At submission, `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, so `proposal_amount_tokens (u64::MAX / E8) ≤ Decimal::MAX` passes unconditionally.
5. Vote the proposal through.
6. At execution, `perform_mint_sns_tokens` calls `self.ledger.transfer_funds(amount_e8s, 0, None, to, memo)` with no limit check.
7. The SNS ledger mints `u64::MAX` e8s of SNS tokens to the attacker's account. [1](#0-0) [9](#0-8)

### Citations

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

**File:** rs/sns/governance/src/governance.rs (L2203-2211)
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L404-416)
```text
message MintSnsTokens {
  // The amount to transfer, in e8s.
  optional uint64 amount_e8s = 1;

  // An optional memo to be used for the transfer.
  optional uint64 memo = 2;

  // The principal to transfer the funds to.
  optional ic_base_types.pb.v1.PrincipalId to_principal = 3;

  // An (optional) Subaccount of the principal to transfer the funds to.
  optional Subaccount to_subaccount = 4;
}
```
