### Title
`MintSnsTokens` 7-Day Minting Cap Not Enforced — (`rs/sns/governance/src/proposal.rs`)

### Summary

The SNS governance canister defines a 7-day rolling minting cap (`mint_sns_tokens_7_day_total_upper_bound_tokens`) for `MintSnsTokens` proposals, analogous to the `supplyCap` in `OmoVault`. The enforcement implementation is explicitly commented out and replaced with a stub returning `Decimal::MAX`, meaning any amount of SNS tokens can be minted via governance proposals with no upper-bound check at either proposal submission or execution time.

---

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` as the protocol-level cap on how many tokens can be minted within a 7-day window. For `TransferSnsTreasuryFunds`, this is properly implemented and enforced at both proposal submission and execution time.

For `MintSnsTokens`, the real implementation is commented out and replaced with a stub:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [1](#0-0) 

The intended implementation — which calls `mint_sns_tokens_7_day_total_upper_bound_tokens` — is blocked behind a `/* TODO(NNS1-2982): Uncomment. */` comment: [2](#0-1) 

The generic enforcement function `treasury_valuation_if_proposal_amount_is_small_enough_or_err` calls `recent_amount_total_upper_bound_tokens` to obtain `max_tokens`, then checks `proposal_amount_tokens > allowance_remainder_tokens`. Since `max_tokens = Decimal::MAX`, the check at line 805 never triggers for `MintSnsTokens`: [3](#0-2) 

Additionally, `perform_mint_sns_tokens` — the execution-time handler — contains no cap check at all: [4](#0-3) 

This contrasts with `perform_transfer_sns_treasury_funds`, which calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` before executing: [5](#0-4) 

The cap function itself (`mint_sns_tokens_7_day_total_upper_bound_tokens`) is fully implemented and correct — it simply is never invoked in the enforcement path: [6](#0-5) 

The integration test confirms the bypass: the assertion that a second over-limit mint proposal should be rejected is commented out, and `unwrap()` is used instead, confirming the proposal succeeds when it should fail: [7](#0-6) 

---

### Impact Explanation

Any SNS governance participant holding sufficient voting power can submit and pass `MintSnsTokens` proposals for arbitrarily large amounts — up to `u64::MAX` e8s — within a single 7-day window, with no protocol-level rejection. The intended cap (e.g., 25% of treasury value for a "medium" SNS) is completely bypassed. This allows unbounded token inflation, diluting all existing SNS token holders. The minting cap is a protocol-level protection designed to be enforced regardless of governance decisions; its absence means the SNS ledger's token supply conservation invariant can be violated at will by any governance majority.

**Impact: High** — unlimited SNS token minting, direct ledger conservation violation.

---

### Likelihood Explanation

The entry path is the standard SNS `make_proposal` ingress call, available to any SNS neuron holder. No special privilege beyond holding a governance majority is required. Early-stage SNS deployments commonly have concentrated voting power (a single whale neuron can hold majority stake). The bypass requires no exploit sophistication — simply submitting a `MintSnsTokens` proposal with a large `amount_e8s` value. The TODO marker and commented-out test confirm this is a known incomplete state in production code.

**Likelihood: High** — trivially reachable by any SNS governance majority; no technical barrier.

---

### Recommendation

1. **Uncomment** the real `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (remove the `/* TODO(NNS1-2982): Uncomment. */` block and delete the stub):

```rust
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {:?}", treasury_limit_error)
        })
}
```

2. **Add an execution-time cap check** in `perform_mint_sns_tokens`, mirroring `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` used in `perform_transfer_sns_treasury_funds`.

3. **Uncomment** the corresponding integration test assertion in `rs/sns/integration_tests/src/sns_treasury.rs` (lines 942–965) and delete the `unwrap()` workaround at line 966.

---

### Proof of Concept

1. Deploy an SNS with a whale neuron holding majority voting power.
2. Submit a `MintSnsTokens` proposal with `amount_e8s = u64::MAX` (or any amount exceeding 25% of treasury value).
3. The proposal passes validation — `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, so the check `proposal_amount_tokens > allowance_remainder_tokens` is always false.
4. After the proposal is adopted, `perform_mint_sns_tokens` executes `ledger.transfer_funds(amount_e8s, 0, None, to, memo)` with no cap check.
5. The SNS ledger mints the full requested amount to the target account, bypassing the intended 7-day rolling cap entirely.

The integration test at `rs/sns/integration_tests/src/sns_treasury.rs:966` explicitly documents this: `doomed_make_proposal_result.unwrap()` — a proposal that should be rejected succeeds. [8](#0-7)

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
