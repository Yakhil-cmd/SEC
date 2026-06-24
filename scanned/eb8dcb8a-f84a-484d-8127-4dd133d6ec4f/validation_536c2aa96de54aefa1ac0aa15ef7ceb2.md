### Title
Disabled `MintSnsTokens` 7-Day Rolling Minting Cap Allows Unbounded SNS Token Supply Inflation — (`rs/sns/governance/src/proposal.rs`)

---

### Summary

The `MintSnsTokens` SNS governance proposal action is supposed to enforce a 7-day rolling upper bound on how many SNS tokens can be minted, derived from the treasury valuation. However, the enforcement function is intentionally commented out and replaced with a stub that returns `Decimal::MAX` — effectively no limit. Any SNS governance participant with sufficient voting power can pass unlimited `MintSnsTokens` proposals, minting an unbounded quantity of SNS tokens and inflating the total supply without constraint.

---

### Finding Description

The `TokenProposalAction` trait in `rs/sns/governance/src/proposal.rs` defines `recent_amount_total_upper_bound_tokens`, which is supposed to cap the 7-day rolling total of minted tokens based on treasury valuation. The correct implementation — calling `mint_sns_tokens_7_day_total_upper_bound_tokens` — is commented out under `TODO(NNS1-2982): Uncomment.`:

```rust
/* TODO(NNS1-2982): Uncomment.
fn recent_amount_total_upper_bound_tokens(valuation: &Valuation) -> Result<Decimal, String> {
    mint_sns_tokens_7_day_total_upper_bound_tokens(*valuation)
        ...
}
*/
```

The active implementation unconditionally returns `Decimal::MAX`:

```rust
// TODO(NNS1-2982): Delete.
fn recent_amount_total_upper_bound_tokens(_valuation: &Valuation) -> Result<Decimal, String> {
    Ok(Decimal::MAX)
}
``` [1](#0-0) 

The enforcement check in `treasury_valuation_if_proposal_amount_is_small_enough_or_err` computes `max_tokens` from this function and rejects proposals where `proposal_amount_tokens > allowance_remainder_tokens`. Since `max_tokens = Decimal::MAX`, the check never triggers: [2](#0-1) 

At execution time, `perform_mint_sns_tokens` calls `self.ledger.transfer_funds(amount_e8s, 0, None, to, mint.memo())` with no additional cap: [3](#0-2) 

The integration test `sns_can_mint_funds_via_proposals` explicitly confirms this: the second minting proposal (which the comment says *should* be rejected) is expected to succeed, and the expected balance is multiplied by 2 to account for both proposals executing: [4](#0-3) 

The `mint_sns_tokens_7_day_total_upper_bound_tokens` function that *would* enforce the cap exists and is correct — it caps minting at 100% of treasury for small treasuries, 25% for medium, and 300,000 XDR worth for large ones — but it is never called for `MintSnsTokens`: [5](#0-4) 

---

### Impact Explanation

An SNS governance participant with sufficient voting power can submit and execute an unlimited number of `MintSnsTokens` proposals, each minting up to `u64::MAX` e8s of SNS tokens. There is no 7-day rolling cap enforced at the protocol level. This allows:

1. **Unbounded SNS token supply inflation** — the total supply can be multiplied arbitrarily, diluting all existing token holders.
2. **Value extraction** — a whale or coordinated group can mint tokens to themselves, dump them on the market, and extract value from other holders.
3. **Governance capture amplification** — an attacker who acquires a governance majority can permanently entrench their position by minting themselves additional tokens.

This is directly analogous to the UDT finding: the intended cap (`maxTokens` / treasury-valuation-based limit) exists in code but is bypassed, allowing token supply to be inflated far beyond what the protocol intends.

---

### Likelihood Explanation

The `MintSnsTokens` proposal action (Action ID 12) is a live, reachable governance action on every deployed SNS. Any SNS neuron holder who can pass a proposal — which in many SNS deployments requires only a single whale neuron with majority voting power — can exploit this. The integration test at `rs/sns/integration_tests/src/sns_treasury.rs:966` literally calls `.unwrap()` on the second minting proposal, confirming the limit is not enforced in the current production code. The TODO comments and ticket references (`NNS1-2982`, `NNS1-2910`) confirm this is a known, unresolved gap.

---

### Recommendation

Uncomment the correct `recent_amount_total_upper_bound_tokens` implementation for `MintSnsTokens` (the block marked `TODO(NNS1-2982): Uncomment`) and delete the stub that returns `Decimal::MAX`. This will enforce the treasury-valuation-based 7-day rolling cap on SNS token minting, consistent with the cap already enforced for `TransferSnsTreasuryFunds` proposals. [6](#0-5) 

---

### Proof of Concept

1. Deploy an SNS with a whale neuron holding majority voting power.
2. Submit a `MintSnsTokens` proposal minting `u64::MAX` e8s to the whale's account.
3. The proposal passes `validate_and_render_mint_sns_tokens` because `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, so `proposal_amount_tokens > allowance_remainder_tokens` is never true.
4. Upon execution, `perform_mint_sns_tokens` calls `self.ledger.transfer_funds(u64::MAX, 0, None, whale_account, memo)` with no cap check.
5. Repeat indefinitely — each proposal mints the maximum amount with no 7-day cooldown enforced.

The integration test at `rs/sns/integration_tests/src/sns_treasury.rs:966` already demonstrates step 3–4 with two consecutive 2,222-token minting proposals both succeeding, and the expected balance being `2 * 2_222` tokens. [7](#0-6)

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

**File:** rs/sns/governance/src/proposal.rs (L1025-1042)
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
}
```

**File:** rs/sns/governance/src/governance.rs (L3084-3086)
```rust
        self.ledger
            .transfer_funds(amount_e8s, 0, None, to, mint.memo())
            .await?;
```

**File:** rs/sns/integration_tests/src/sns_treasury.rs (L942-983)
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

    // Whale's balance is not affected by the second proposal.
    let balance = icrc1_balance(
        &state_machine,
        sns_ledger_canister_id,
        Account {
            owner: Principal::from(*WHALE),
            subaccount: None,
        },
    );
    let expected_balance_tokens = Tokens::new(
        2 * // TODO(NNS1-2982): Delete this line.
        2_222,
        0,
    )
    .unwrap();
    assert_eq!(balance, expected_balance_tokens);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L14-18)
```rust
pub fn mint_sns_tokens_7_day_total_upper_bound_tokens(
    valuation: Valuation,
) -> Result<Decimal, ProposalsAmountTotalLimitError> {
    ProposalsAmountTotalUpperBound::in_tokens(valuation)
}
```
