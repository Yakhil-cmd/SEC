### Title
Disabled `MintSnsTokens` 7-Day Rate Limit Allows Unlimited SNS Token Minting via Governance Proposal - (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The `MintSnsTokens` SNS governance proposal action has its 7-day rate-limiting upper bound intentionally disabled via a `TODO` comment, and `perform_mint_sns_tokens` performs no execution-time amount check. Any neuron holder who can pass a `MintSnsTokens` proposal can mint an **arbitrary, unbounded** amount of SNS tokens to any recipient principal, with no protocol-level guard enforced.

---

### Finding Description

The SNS governance canister defines two token-moving proposal types: `TransferSnsTreasuryFunds` and `MintSnsTokens`. Both are supposed to be rate-limited by a 7-day rolling window cap tied to treasury valuation.

For `TransferSnsTreasuryFunds`, the protection is fully implemented:

1. **At submission time**: `validate_and_render_transfer_sns_treasury_funds` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which calls `TransferSnsTreasuryFunds::recent_amount_total_upper_bound_tokens` → `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens` → a real XDR-based cap.
2. **At execution time**: `perform_transfer_sns_treasury_funds` calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` before transferring.

For `MintSnsTokens`, **both guards are missing or disabled**:

**Guard 1 — Submission-time upper bound is `Decimal::MAX`:**

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    // Ideally, we'd return infinity, but Decimal does not have that. This is the next best
    // thing, and should be good enough, because we have already planned the obselences of this
    // code (see tickets NNS1-298(1|2)).
    Ok(Decimal::MAX)
}
```

The real implementation is commented out:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
    ...
}
*/
```

Because `max_tokens = Decimal::MAX`, the check `if proposal_amount_tokens > allowance_remainder_tokens` in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` **never triggers**, regardless of the proposed mint amount.

**Guard 2 — No execution-time check in `perform_mint_sns_tokens`:**

```rust
async fn perform_mint_sns_tokens(
    &mut self,
    mint: MintSnsTokens,
) -> Result<(), GovernanceError> {
    ...
    self.ledger
        .transfer_funds(amount_e8s, 0, None, to, mint.memo())
        .await?;
    Ok(())
}
```

There is no call to any `mint_sns_tokens_amount_is_small_enough_at_execution_time_or_err` equivalent. Compare this to `perform_transfer_sns_treasury_funds`, which calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` before executing.

The integration test explicitly confirms the limit is not enforced and marks the correct behavior as a TODO:

```rust
/* TODO(NNS1-2982): Uncomment.
let err = doomed_make_proposal_result.unwrap_err();
...
*/
doomed_make_proposal_result.unwrap(); // TODO(NNS1-2982): Delete this line.
```

---

### Impact Explanation

A neuron holder (or coalition) with sufficient voting power to pass a `MintSnsTokens` proposal can mint an **unbounded** quantity of SNS tokens to any recipient. This:

- Inflates the SNS token supply without limit, diluting all existing token holders.
- Allows a whale or coordinated group to extract value equivalent to the entire SNS treasury by minting tokens and selling them.
- Bypasses the protocol-level conservation guarantee that was explicitly designed to cap 7-day minting to a fraction of treasury value.

The `MintSnsTokens` action mints from the SNS ledger's minting account (subaccount `None`, fee `0`), meaning it creates new tokens rather than moving existing ones — the impact is unbounded inflation, not just treasury drain.

**Impact: High** — unlimited token supply inflation, value extraction from all existing holders.

---

### Likelihood Explanation

`MintSnsTokens` is a **critical proposal** (topic `TreasuryAssetManagement`, `is_critical: true`), requiring a higher voting threshold. However:

- A whale neuron holder with dominant voting power (common in early-stage SNS DAOs) can unilaterally pass such proposals.
- The disabled guard is a known, documented gap (TODO tickets NNS1-2982, NNS1-2910) — it is not a latent bug but an acknowledged missing protection.
- The attacker entry path is a standard ingress call to `manage_neuron` (to make the proposal) followed by governance voting — fully reachable by any unprivileged principal with a neuron.

**Likelihood: Medium** — requires governance majority, but the protection that was supposed to prevent this is explicitly disabled.

---

### Recommendation

1. **Immediately uncomment** the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (tracked as TODO NNS1-2982) and delete the `Decimal::MAX` stub.
2. **Add an execution-time check** in `perform_mint_sns_tokens` analogous to `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err`, using the `MintSnsTokensActionAuxiliary` valuation stored in `action_auxiliary`.
3. **Uncomment** the integration test assertion in `sns_can_mint_funds_via_proposals` that verifies the second mint proposal is rejected.

---

### Proof of Concept

**Step 1**: A whale neuron holder submits a `MintSnsTokens` proposal via `manage_neuron`:

```
Action::MintSnsTokens(MintSnsTokens {
    amount_e8s: Some(u64::MAX),   // entire u64 range — no limit enforced
    to_principal: Some(attacker_principal),
    to_subaccount: None,
    memo: None,
})
```

**Step 2**: At submission, `validate_and_render_mint_sns_tokens` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`. The check `proposal_amount_tokens > allowance_remainder_tokens` evaluates as `(u64::MAX / E8) > (Decimal::MAX - 0)` → **false** → proposal is accepted.

**Step 3**: The proposal passes the critical-proposal voting threshold (whale has enough voting power).

**Step 4**: `perform_mint_sns_tokens` is called. It calls `self.ledger.transfer_funds(u64::MAX, 0, None, attacker_account, 0)` with no amount check. The SNS ledger mints `u64::MAX` e8s of SNS tokens to the attacker.

**Relevant code locations**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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
