### Title
SNS Treasury Transfer Limit Bypassed via Live Balance Inflation at Proposal Submission — (File: `rs/sns/governance/src/proposal.rs`)

---

### Summary

The 7-day `TransferSnsTreasuryFunds` transfer limit is computed using the **live** treasury balance read at proposal submission time. Because any token holder can send tokens to the SNS treasury account (a standard ledger transfer requiring no special permission), an attacker can temporarily inflate the treasury balance before submitting a proposal, locking in an inflated limit. The inflated valuation is stored at submission time and reused at execution time without a fresh balance read, allowing the proposal to transfer more tokens than the safety limit was designed to permit.

---

### Finding Description

In `rs/sns/governance/src/proposal.rs`, the function `treasury_valuation_if_proposal_amount_is_small_enough_or_err` calls `assess_treasury_balance`, which calls `Token::assess_balance`, which calls `try_get_balance_valuation_factors`, which issues a live `icrc1_balance_of` query to the ledger canister to read the current treasury balance. [1](#0-0) 

This live balance is fed into `ProposalsAmountTotalUpperBound::in_tokens` in `rs/sns/governance/proposals_amount_total_limit/src/lib.rs`, which classifies the treasury into one of three regimes based on its XDR value:

- **Small** (≤ 100,000 XDR) → `NoLimit`: the entire balance may be transferred.
- **Medium** (≤ 1,200,000 XDR) → `Fraction(0.25)`: at most 25% of the balance.
- **Large** (> 1,200,000 XDR) → `Xdr(300,000)`: at most 300,000 XDR worth. [2](#0-1) 

The resulting `Valuation` is stored in `ActionAuxiliary::TransferSnsTreasuryFunds(valuation)` at proposal submission time. [3](#0-2) 

At execution time, `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` reuses this **stored** valuation — it does not re-read the treasury balance. [4](#0-3) 

The live balance read in `try_get_balance_valuation_factors` is the manipulable variable: [5](#0-4) 

Because the SNS treasury is a standard ICRC-1 ledger account, **any token holder can send tokens to it without any permission**. This inflates the balance, potentially pushing the treasury from one regime to another, and the inflated limit is locked in at proposal submission time.

---

### Impact Explanation

**Concrete example (medium → large regime shift):**

1. Treasury holds 110,000 SNS tokens at 10 XDR/token = 1,100,000 XDR → **medium** regime, normal limit = 25% × 110,000 = **27,500 tokens**.
2. Attacker sends 10,001 tokens to the treasury → treasury now holds 120,001 tokens = 1,200,010 XDR → **large** regime, limit = 300,000 XDR ÷ 10 XDR/token = **30,000 tokens**.
3. Attacker submits a `TransferSnsTreasuryFunds` proposal for 30,000 tokens. The proposal passes the submission-time check (30,000 ≤ 30,000).
4. Governance voters see the proposal is within the displayed limit and approve it.
5. Proposal executes: 30,000 tokens transferred to the attacker.
6. Attacker net: donated 10,001 tokens, received 30,000 tokens → extracted **2,500 tokens beyond the intended limit** from the treasury, at a cost of 10,001 tokens donated permanently.

The safety mechanism — designed to prevent rapid treasury drainage — is undermined. Governance voters are deceived: they see a proposal that appears to be within the limit, but the limit itself was manipulated. The stored valuation at execution time reflects the inflated balance, not the true treasury state. [6](#0-5) 

---

### Likelihood Explanation

**Medium.** The manipulation step (sending tokens to the treasury) requires no special permission — any token holder can perform it via a standard ICRC-1 transfer. The attacker must also have enough tokens to inflate the treasury and must coordinate with (or be) a governance majority to pass the proposal. However, the manipulation is trivially executed on-chain, and the inflated limit is invisible to governance voters who rely on the system's limit check. SNS DAOs with a dominant whale neuron holder are particularly at risk, as a single actor can both inflate the treasury and pass the proposal.

---

### Recommendation

**Short term:** Do not use the live treasury balance to compute the transfer limit at proposal submission time. Instead, use a snapshot of the treasury balance taken at a fixed point (e.g., the most recent heartbeat or a certified state read), or compute the limit based on the balance **minus** the proposal amount itself, so that inflating the treasury does not increase the limit beyond what was already there.

**Long term:** Review all governance proposal validation logic that reads live external state (ledger balances, price feeds) to determine limits or permissions. Any such computation is susceptible to manipulation by actors who can influence that state. Consider using time-weighted average balances or requiring the limit to be computed from a certified, tamper-evident snapshot rather than a live query.

---

### Proof of Concept

**Attacker-controlled entry path:**

1. **Inflate:** Attacker calls `icrc1_transfer` on the SNS ledger (or ICP ledger for ICP treasury), sending tokens to the SNS governance canister's treasury account. No permission required.
2. **Submit:** Attacker (or any neuron holder) calls `manage_neuron` → `MakeProposal` with a `TransferSnsTreasuryFunds` action whose `amount_e8s` exceeds the normal limit but is within the inflated limit. `validate_and_render_transfer_sns_treasury_funds` calls `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which reads the inflated balance via `assess_treasury_balance` → `icrc1_balance_of`. The proposal passes validation and the inflated `Valuation` is stored in `ActionAuxiliary`.
3. **Vote:** Governance voters approve the proposal. They see it passed the limit check.
4. **Execute:** `perform_transfer_sns_treasury_funds` calls `transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err` with the stored (inflated) valuation. The check passes. The transfer executes, moving more tokens than the intended limit to the attacker. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L571-594)
```rust
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        transfer,
    )
    .await;
    let valuation = match valuation {
        Ok(ok) => Some(ok),
        Err(err) => {
            defects.push(err);
            None
        }
    };

    // Validate all other aspects of the proposal action.
    locally_validate_and_render_transfer_sns_treasury_funds(transfer, sns_transfer_fee_e8s, defects)
        .and_then(|rendering| {
            match valuation {
                Some(valuation) => Ok((
                    rendering,
                    ActionAuxiliary::TransferSnsTreasuryFunds(valuation),
                )),
```

**File:** rs/sns/governance/src/proposal.rs (L782-790)
```rust
    // Get valuation of the tokens in the treasury.
    let token = action.token()?;
    let valuation = assess_treasury_balance(
        token,
        env.canister_id(),
        sns_ledger_canister_id,
        swap_canister_id,
    )
    .await?;
```

**File:** rs/sns/governance/src/proposal.rs (L2600-2617)
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
```

**File:** rs/sns/governance/src/proposal.rs (L2644-2656)
```rust
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
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L76-87)
```rust
        let result_tokens = match self_ {
            Self::NoLimit => balance_tokens,

            Self::Fraction(fraction) => balance_tokens
                .checked_mul(fraction)
                // Overflow should not be possible, since fraction is supposed to be at most 1.0.
                .ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "Unable to perform {balance_tokens} * {fraction}.",
                    ))
                })?,

```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L116-135)
```rust
    fn from_valuation_xdr(valuation_xdr: Decimal) -> Self {
        // Ideally, this would be checked at compile time. In principal, this should be possible,
        // since all the inputs are const, but I'm not sure how to do that. Therefore,
        // debug_assert_eq is used instead, and should be very nearly as good, because this will be
        // run during CI.
        debug_assert_eq!(
            Self::MAX_MEDIUM_TREASURY_SIZE_XDR.checked_mul(ONE_QUARTER),
            Some(Self::MAX_XDR),
        );

        if valuation_xdr <= Self::MAX_SMALL_TREASURY_SIZE_XDR {
            return Self::NoLimit;
        }

        if valuation_xdr <= Self::MAX_MEDIUM_TREASURY_SIZE_XDR {
            return Self::Fraction(ONE_QUARTER);
        }

        Self::Xdr(Self::MAX_XDR)
    }
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L154-181)
```rust
    let balance_of_request = icrc1_client.icrc1_balance_of(account);
    let icps_per_token_request = icps_per_token_client.get();
    let xdrs_per_icp_request = xdrs_per_icp_client.get();

    // Make all (3) requests (concurrently).
    let (balance_of_response, icps_per_token_response, xdrs_per_icp_response) = join!(
        balance_of_request,
        icps_per_token_request,
        xdrs_per_icp_request,
    );

    // Unwrap/forward errors to the caller.
    let balance_of_response = balance_of_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to obtain balance from ledger: {err:?}"))
    })?;
    let icps_per_token_response = icps_per_token_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to determine ICPs per token: {err:?}"))
    })?;
    let xdrs_per_icp_response = xdrs_per_icp_response.map_err(|err| {
        ValuationError::new_external(format!("Unable to obtain XDR per ICP: {err:?}"))
    })?;

    // Extract and interpret the data we actually care about from the (Ok) responses.
    let tokens = Decimal::from(u128::try_from(balance_of_response.0).map_err(|err| {
        ValuationError::new_arithmetic(format!(
            "Balance of {account:?} does not fit in u128: {err:?}"
        ))
    })?) / Decimal::from(E8);
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
