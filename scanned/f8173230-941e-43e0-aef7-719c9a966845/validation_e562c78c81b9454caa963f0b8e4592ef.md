### Title
Disabled Minting Cap Allows Unlimited SNS Token Minting via `MintSnsTokens` Proposals - (`rs/sns/governance/src/proposal.rs`)

### Summary
The SNS governance canister intentionally disables the 7-day minting cap for `MintSnsTokens` proposals by returning `Decimal::MAX` as the upper bound, allowing an SNS community to pass an unlimited number of minting proposals within any 7-day window. This is the direct IC analog of the "no cap on borrowable Note" vulnerability: just as the Canto lending market had no ceiling on how much Note could be minted/borrowed, the SNS governance has no enforced ceiling on how many SNS tokens can be minted via governance proposals.

### Finding Description

The `TokenProposalAction` trait defines `recent_amount_total_upper_bound_tokens` to limit how many tokens can be minted within a rolling 7-day window. For `TransferSnsTreasuryFunds`, this limit is enforced via `transfer_sns_treasury_funds_7_day_total_upper_bound_tokens`, which caps transfers based on treasury valuation (100% for small, 25% for medium, 300,000 XDR for large treasuries).

However, for `MintSnsTokens`, the enforcement is explicitly commented out and replaced with `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
```

The correct implementation — `mint_sns_tokens_7_day_total_upper_bound_tokens` — exists in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs` and is fully implemented, but is commented out at the call site:

```rust
// TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
```

The integration test `sns_can_mint_funds_via_proposals` in `rs/sns/integration_tests/src/sns_treasury.rs` explicitly documents that the second minting proposal **should** fail but currently **succeeds**, with the assertion block commented out and replaced by `.unwrap()`.

The `validate_and_render_mint_sns_tokens` function does call `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which in turn calls `recent_amount_total_upper_bound_tokens` — but since that returns `Decimal::MAX`, the check is always satisfied regardless of the proposed mint amount.

### Impact Explanation

Any SNS with a sufficiently large neuron majority can pass back-to-back `MintSnsTokens` proposals with no per-window cap. This allows:

1. **Unlimited token inflation**: An SNS governance majority can mint arbitrarily large amounts of SNS tokens to any principal within a 7-day window, diluting all existing token holders to near-zero.
2. **Treasury drain via inflation**: Minted tokens can be immediately used to acquire ICP from the SNS treasury via the swap canister, draining the ICP treasury.
3. **Governance takeover**: Minted tokens can be staked as neurons to seize voting majority, enabling further malicious proposals.

The impact is equivalent to the Canto finding: the Oracle Extractable Value (here: the value extractable by a governance majority) is unbounded because there is no cap on how much can be minted.

### Likelihood Explanation

The entry path is a standard SNS governance proposal submitted by any principal holding sufficient voting power. No oracle manipulation or external attack is required — only a governance majority. For SNS DAOs with concentrated token holdings (e.g., a whale neuron), this is a realistic scenario. The code explicitly acknowledges this is a known gap (ticket NNS1-2982) that has not yet been closed.

### Recommendation

Uncomment the real enforcement in `rs/sns/governance/src/proposal.rs`:

```rust
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        .map_err(|treasury_limit_error| {
            format!("Unable to validate amount: {:?}", treasury_limit_error)
        })
}
```

And import `mint_sns_tokens_7_day_total_upper_bound_tokens` (currently commented out). Also uncomment the corresponding integration test assertion in `rs/sns/integration_tests/src/sns_treasury.rs`.

### Proof of Concept

1. Deploy an SNS with a whale neuron holding majority voting power.
2. Submit a `MintSnsTokens` proposal minting, e.g., 1,000,000 SNS tokens to the whale's principal.
3. The proposal passes `validate_and_render_mint_sns_tokens`, which calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`. This calls `recent_amount_total_upper_bound_tokens`, which returns `Decimal::MAX`. The check `proposal_amount_tokens > allowance_remainder_tokens` is always false.
4. The proposal is accepted and executed, minting 1,000,000 tokens with no limit.
5. Repeat immediately with another proposal for another 1,000,000 tokens — no 7-day window restriction applies.
6. The integration test at line 966 of `rs/sns/integration_tests/src/sns_treasury.rs` confirms this: `doomed_make_proposal_result.unwrap()` — the second mint succeeds when it should fail. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L51-54)
```rust
use ic_sns_governance_proposals_amount_total_limit::{
    // TODO(NNS1-2982): Uncomment. mint_sns_tokens_7_day_total_upper_bound_tokens,
    transfer_sns_treasury_funds_7_day_total_upper_bound_tokens,
};
```

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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L126-134)
```rust
        if valuation_xdr <= Self::MAX_SMALL_TREASURY_SIZE_XDR {
            return Self::NoLimit;
        }

        if valuation_xdr <= Self::MAX_MEDIUM_TREASURY_SIZE_XDR {
            return Self::Fraction(ONE_QUARTER);
        }

        Self::Xdr(Self::MAX_XDR)
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
