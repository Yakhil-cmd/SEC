### Title
SNS Token Burn Inflates Treasury Valuation, Blocking Legitimate `TransferSnsTreasuryFunds` Proposals - (File: rs/sns/governance/token_valuation/src/lib.rs)

### Summary

The `IcpsPerSnsTokenClient::fetch_icps_per_sns_token` function computes the current SNS token price by dividing the initial swap price by a `total_inflation` factor derived from `icrc1_total_supply`. Because any unprivileged token holder can burn SNS tokens (reducing `icrc1_total_supply`), an attacker can artificially deflate `total_inflation`, inflate the computed `icps_per_token`, and thereby inflate the treasury's XDR valuation. This pushes the treasury into a more restrictive `ProposalsAmountTotalUpperBound` regime, blocking legitimate `TransferSnsTreasuryFunds` governance proposals.

### Finding Description

`fetch_icps_per_sns_token` in `rs/sns/governance/token_valuation/src/lib.rs` computes the current SNS token price as:

```
current_icps_per_sns_token = initial_icps_per_sns_token / (current_supply / initial_supply)
``` [1](#0-0) 

`initial_supply` is determined by `initial_supply_e8s`, which scans the ledger from block 0 and sums all mint transactions sharing the same timestamp as the first transaction. [2](#0-1) 

`current_supply` is fetched live via `icrc1_total_supply`. [3](#0-2) 

In ICRC-1, any token holder can burn tokens by transferring to the minting account. This reduces `icrc1_total_supply`. When `current_supply < initial_supply`, `total_inflation < 1`, and dividing by a number less than 1 produces `current_icps_per_sns_token > initial_icps_per_sns_token`. The inflated price propagates into the treasury valuation:

```
treasury_value_xdr = treasury_balance_tokens × icps_per_token × xdrs_per_icp
``` [4](#0-3) 

This inflated valuation is then used by `ProposalsAmountTotalUpperBound::in_tokens` to determine the 7-day transfer limit for `TransferSnsTreasuryFunds` proposals: [5](#0-4) 

The three regimes are:
- **Small** (< 100,000 XDR): `NoLimit` — full balance transferable
- **Medium** (100,000–1,200,000 XDR): `Fraction(0.25)` — 25% per 7 days
- **Large** (> 1,200,000 XDR): `Xdr(300,000)` — at most 300,000 XDR worth per 7 days [6](#0-5) 

By burning enough tokens to inflate `icps_per_token`, an attacker can push a "small" treasury (NoLimit) into the "large" regime, where the per-7-day token limit becomes `300,000 / (inflated_xdrs_per_token)` — an arbitrarily small number. The valuation is checked both at proposal submission and at execution time: [7](#0-6) [8](#0-7) 

There is no floor on `icps_per_token` (only a floor on `xdrs_per_icp` at 1 XDR/ICP), so the inflation of the computed price is unbounded. [9](#0-8) 

### Impact Explanation

An attacker holding SNS tokens can burn them to inflate the computed treasury valuation, moving the treasury from the "small" (NoLimit) regime to the "large" (300,000 XDR cap) regime. This blocks legitimate `TransferSnsTreasuryFunds` governance proposals that would otherwise be permitted. The attacker also benefits financially by preventing treasury disbursements that would dilute the value of their remaining holdings. The effect persists until the SNS governance mints new tokens or the natural inflation from staking rewards restores the supply ratio — which may take a long time.

### Likelihood Explanation

Any SNS token holder can execute this attack by calling `icrc1_transfer` on the SNS ledger with the minting account as the destination (standard ICRC-1 burn). No privileged access, admin key, or governance majority is required. The attacker only needs to hold enough tokens to shift the treasury valuation across a regime boundary. For a treasury near the 100,000 XDR boundary, a relatively small burn may suffice. The attack is cheap relative to the disruption caused.

### Recommendation

1. **Clamp `icps_per_token`**: Introduce a `MIN_ICPS_PER_TOKEN` and `MAX_ICPS_PER_TOKEN` analogous to the existing `MIN_XDRS_PER_ICP` floor, so that extreme supply changes do not produce unbounded price inflation.

2. **Use a time-weighted or smoothed supply**: Instead of using the instantaneous `icrc1_total_supply`, use a moving average or a supply snapshot taken at a fixed recent block, making single-block burns insufficient to manipulate the valuation.

3. **Separate the inflation adjustment from the security-critical limit**: The inflation-adjusted price model is an approximation ("not an exact science" per the code comments). Security-critical treasury limits should not rely solely on this approximation without additional safeguards.

### Proof of Concept

Assume an SNS with:
- `initial_supply = 1,000,000` tokens (at genesis)
- `initial_sns_tokens_per_icp = 100` (i.e., `initial_icps_per_sns_token = 0.01`)
- `xdrs_per_icp = 10`
- Treasury balance = 100,000 tokens

**Before attack:**
- `current_supply = 1,000,000`
- `total_inflation = 1,000,000 / 1,000,000 = 1.0`
- `icps_per_token = 0.01 / 1.0 = 0.01`
- `treasury_value_xdr = 100,000 × 0.01 × 10 = 10,000 XDR` → **Small** → NoLimit → full 100,000 tokens transferable

**Attacker burns 900,000 of their own tokens:**
- `current_supply = 100,000`
- `total_inflation = 100,000 / 1,000,000 = 0.1`
- `icps_per_token = 0.01 / 0.1 = 0.1`
- `treasury_value_xdr = 100,000 × 0.1 × 10 = 100,000 XDR` → **Medium** → 25% limit → only 25,000 tokens transferable per 7 days

**Attacker burns 990,000 tokens (more aggressive):**
- `current_supply = 10,000`
- `total_inflation = 0.01`
- `icps_per_token = 1.0`
- `treasury_value_xdr = 100,000 × 1.0 × 10 = 1,000,000 XDR` → **Large** → `300,000 / (1.0 × 10) = 30,000` tokens per 7 days

The attacker executes this by calling `icrc1_transfer` on the SNS ledger canister with `to = minting_account` and `amount = 900_000 * E8`, which is a standard ICRC-1 burn requiring no special permissions.

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L117-127)
```rust
impl ValuationFactors {
    pub fn to_xdr(&self) -> Decimal {
        let Self {
            tokens,
            icps_per_token,
            xdrs_per_icp,
        } = self;

        tokens * icps_per_token * xdrs_per_icp
    }
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L325-330)
```rust
            MyRuntime::call_with_cleanup::<_, (Nat,)>(
                self.sns_token_ledger_canister_id,
                "icrc1_total_supply",
                ()
            ),
        );
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L397-414)
```rust
        let total_inflation = current_supply_e8s
            .checked_div(initial_supply_e8s)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to perform current_supply / initial_supply \
                     (where current_supply_e8s = {current_supply_e8s} and initial_supply_e8s = {initial_supply_e8s})",
                ))
            })?;

        // Finally, current price = initial price scaled down by inflation (or deflation).
        initial_icps_per_sns_token
            .checked_div(total_inflation)
            .ok_or_else(|| {
                ValuationError::new_arithmetic(format!(
                    "Unable to perform initial_icps_per_sns_token / total_inflation \
                     (where initial_icps_per_sns_token = {initial_icps_per_sns_token} and total_inflation = {total_inflation})",
                ))
            })
```

**File:** rs/nervous_system/initial_supply/src/lib.rs (L46-92)
```rust
        for transaction in transactions {
            // Look at timestamp. If != first_timestamp, we are done.
            match first_timestamp {
                None => {
                    first_timestamp = Some(transaction.timestamp);
                }
                Some(first_timestamp) => {
                    if transaction.timestamp != first_timestamp {
                        // Found a non-initial transaction -> Done!
                        break 'outer;
                    }
                }
            }
            debug_assert_eq!(Some(transaction.timestamp), first_timestamp);

            // Bail if this scan seems to go on forever.
            if transaction_count >= max_transactions {
                return Err(format!(
                    "Unable to find the last initial transaction after scanning {transaction_count} transactions.",
                ));
            }

            if transaction.kind != "mint" {
                // This is pretty weird, but not impossible that a non-mint with
                // the same block timestamp as the first transaction, but if
                // this does happen, then, we define the all the mint
                // transactions prior to this transaction to be the "initial
                // supply".
                break 'outer;
            }

            // Unpack transaction; it should be a mint.
            let mint = match transaction.mint {
                Some(ok) => ok,
                None => {
                    return Err(format!(
                        "Transaction {transaction_count} was not a mint, even though its kind is \"mint\": {transaction:#?}",
                    ));
                }
            };

            // Update running totals.
            result.add_assign(mint.amount);
            transaction_count = transaction_count
                .checked_add(1)
                .ok_or_else(|| "Transaction count overflowed u64.".to_string())?;
        }
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L34-64)
```rust
impl ProposalsAmountTotalUpperBound {
    // A treasury can be small, medium, or large. These are the boundaries between those regimes.
    const MAX_SMALL_TREASURY_SIZE_XDR: Decimal = dec!(100_000);
    const MAX_MEDIUM_TREASURY_SIZE_XDR: Decimal = dec!(1_200_000);

    // No matter how large the treasury is, not more than this amount can be removed (within a 7 day
    // window).
    const MAX_XDR: Decimal = dec!(300_000);

    /// A price quote less than this is considered "unrealistically" low. When that happens, we use
    /// this instead of the quoted value.
    ///
    /// # Motivation
    ///
    /// Low XDRs per ICP quotes would tend to cause our valuations to be in the "small" regime,
    /// where an SNS is allowed to take the biggest actions relative to their size. This is to
    /// minmize the damage caused by wacky price quotes.
    ///
    /// # What Value to Use
    ///
    /// Currently, the minimum XDRs per ICP used by NNS governance is 1. This is simply copied from
    /// there, specifically from the minimum_icp_xdr_rate field in NetworkEconomics.
    ///
    /// As of Mar 2024, the price of ICP is around 10 XDR. The lowest it has ever been is around 2.2
    /// XDR. FWIW, this is less than that.
    ///
    /// # Why Not Also Define MAX?
    ///
    /// Currently, we do not have/enforce a MAX_XDRS_PER_ICP, because this would tend to cause our
    /// valuations to be in the "large" regime, where actions are more limited.
    const MIN_XDRS_PER_ICP: Decimal = dec!(1);
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L66-114)
```rust
    fn in_tokens(mut valuation: Valuation) -> Result<Decimal, ProposalsAmountTotalLimitError> {
        Self::clamp_xdrs_per_icp(&mut valuation);

        let ValuationFactors {
            tokens: balance_tokens,
            icps_per_token,
            xdrs_per_icp,
        } = valuation.valuation_factors;

        let self_ = Self::from_valuation_xdr(valuation.to_xdr());
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

            Self::Xdr(max_xdr) => {
                let xdrs_per_token = xdrs_per_icp.checked_mul(icps_per_token).ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "XDRs per token could not be calculated from valuation: {valuation:?}"
                    ))
                })?;

                // Calculate the inverse conversion rate.
                if xdrs_per_token == Decimal::from(0) {
                    // This is not reachable, because in this case, valuation.to_xdr() would return
                    // 0, and in that case, we would have taken the NoLimit branch.
                    return Err(ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "It appears that the tokens have zero value in XDR. valuation = {valuation:?}"
                    )));
                }
                let tokens_per_xdr = xdrs_per_token.inv();

                max_xdr.checked_mul(tokens_per_xdr).ok_or_else(|| {
                    ProposalsAmountTotalLimitError::new_arithmetic(format!(
                        "Max tokens could not be calculated with valuation: {valuation:?}",
                    ))
                })?
            }
        };

        Ok(result_tokens)
    }
```

**File:** rs/sns/governance/src/proposal.rs (L770-817)
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
