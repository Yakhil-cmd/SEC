### Title
`MintSnsTokens` Proposal Action Has No Effective 7-Day Supply Cap Enforcement - (`rs/sns/governance/src/proposal.rs`)

### Summary
The SNS Governance `MintSnsTokens` proposal action intentionally returns `Decimal::MAX` as its minting upper bound, completely disabling the 7-day cumulative minting limit that was designed to protect SNS token holders from unlimited dilution. The analogous `TransferSnsTreasuryFunds` action has proper enforcement, but `MintSnsTokens` does not.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to cap the 7-day total of minting or treasury-transfer proposals. For `TransferSnsTreasuryFunds`, this is properly implemented: [1](#0-0) 

For `MintSnsTokens`, the real implementation is commented out and replaced with a stub that returns `Decimal::MAX`: [2](#0-1) 

The validation path for `MintSnsTokens` proposals calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which computes `max_tokens` from `recent_amount_total_upper_bound_tokens` and checks `proposal_amount_tokens > allowance_remainder_tokens`: [3](#0-2) 

Since `max_tokens` is always `Decimal::MAX`, the check `proposal_amount_tokens > Decimal::MAX` is always `false`, meaning **any amount passes validation**.

At execution time, `perform_mint_sns_tokens` performs no supply-limit check whatsoever — it directly calls `transfer_funds`: [4](#0-3) 

By contrast, `TransferSnsTreasuryFunds` has a second execution-time guard `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`: [5](#0-4) 

No equivalent guard exists for `MintSnsTokens`.

The integration test explicitly confirms this is a live issue — the assertion that a second over-limit mint proposal should fail is commented out, and the test currently asserts it **succeeds**: [6](#0-5) 

### Impact Explanation

An SNS neuron holder with sufficient voting power can submit `MintSnsTokens` proposals for arbitrarily large amounts — including amounts exceeding the entire existing token supply — with no 7-day cap enforced at either proposal submission or execution time. This allows unlimited SNS token inflation through the governance mechanism, diluting all existing token holders without bound. The ledger's `mint` function itself has no supply cap: [7](#0-6) 

**Impact: Medium** — SNS token conservation is broken for the `MintSnsTokens` action; unlimited dilution is possible through governance.

### Likelihood Explanation

**Likelihood: Medium** — Requires an SNS governance participant to accumulate sufficient voting power to pass proposals (a normal governance operation, not a privileged role). The `MintSnsTokens` proposal type is a standard, publicly documented SNS action. Any SNS with a concentrated neuron distribution (e.g., a whale holding >50% of voting power, or a coordinated group) can exploit this immediately. The commented-out TODO (`NNS1-2982`) confirms the developers are aware the limit is not enforced.

### Recommendation

1. Uncomment the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` that calls `mint_sns_tokens_7_day_total_upper_bound_tokens`: [8](#0-7) 

2. Add an execution-time check analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` inside `perform_mint_sns_tokens`, using the `ActionAuxiliary::MintSnsTokens` valuation stored at proposal submission time.

3. Delete the `Decimal::MAX` stub and the `#[allow(unused)]` annotation on `total_minting_amount_tokens`.

### Proof of Concept

1. Deploy an SNS with a whale neuron holding majority voting power.
2. Submit a `MintSnsTokens` proposal with `amount_e8s = u64::MAX` (≈184 billion tokens).
3. Observe that `validate_and_render_mint_sns_tokens` accepts the proposal because `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`.
4. Pass the proposal through normal governance voting.
5. `perform_mint_sns_tokens` executes with no supply check, minting `u64::MAX` e8s to the attacker's account.
6. Repeat indefinitely — there is no cumulative cap and no execution-time guard.

The integration test at `rs/sns/integration_tests/src/sns_treasury.rs:966` already demonstrates step 3–5: `doomed_make_proposal_result.unwrap()` succeeds where it should fail.

### Citations

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

**File:** rs/ledger_suite/common/ledger_core/src/balances.rs (L145-156)
```rust
    pub fn mint(
        &mut self,
        to: &S::AccountId,
        amount: S::Tokens,
    ) -> Result<(), BalanceError<S::Tokens>> {
        self.token_pool = self
            .token_pool
            .checked_sub(&amount)
            .expect("total token supply exceeded");
        self.credit(to, amount);
        Ok(())
    }
```
