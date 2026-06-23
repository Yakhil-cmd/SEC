### Title
SNS Token Treasury Valuation Uses Manipulable Spot Supply to Compute `icps_per_token`, Enabling Governance Limit Bypass - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

### Summary

The SNS governance treasury-transfer rate-limiting system computes the XDR value of the SNS token treasury by calling `IcpsPerSnsTokenClient::fetch_icps_per_sns_token`. This function derives the current SNS token price by taking the swap's initial `sns_tokens_per_icp` ratio and deflating it by `current_supply / initial_supply`. Because `current_supply` is read live from `icrc1_total_supply` at proposal-submission time, an attacker who can inflate the on-chain total supply (e.g., via a `MintSnsTokens` governance proposal that currently has **no enforced upper bound**) can artificially depress the computed `icps_per_token`, which in turn depresses the treasury's XDR valuation, which in turn pushes the treasury into the "small" regime where **no limit** applies to `TransferSnsTreasuryFunds` proposals.

### Finding Description

**Pricing path for SNS token treasury:**

`assess_treasury_balance` → `try_get_sns_token_balance_valuation` → `IcpsPerSnsTokenClient::fetch_icps_per_sns_token`

Inside `fetch_icps_per_sns_token`, the price is computed as:

```
current_icps_per_sns_token = (1 / initial_sns_tokens_per_icp) / (current_supply / initial_supply)
``` [1](#0-0) 

The `current_supply_e8s` is fetched live via `icrc1_total_supply` at the moment a `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal is submitted: [2](#0-1) 

The resulting `ValuationFactors` is then used to classify the treasury as "small" (≤ 100,000 XDR → `NoLimit`), "medium" (≤ 1,200,000 XDR → 25% cap), or "large" (> 1,200,000 XDR → 300,000 XDR cap): [3](#0-2) 

When the treasury is classified as `NoLimit`, the full treasury balance is returned as the allowed transfer amount: [4](#0-3) 

**The critical gap:** `MintSnsTokens` proposals currently have **no enforced upper bound** — `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX` unconditionally: [5](#0-4) 

This is explicitly marked as a TODO to be fixed (ticket NNS1-2982), but is currently live in production code.

**Attack chain:**

1. Attacker controls enough SNS voting power to pass proposals (or exploits the window between proposal submission and execution).
2. Attacker submits a `MintSnsTokens` proposal to mint a very large number of SNS tokens to themselves. Because `recent_amount_total_upper_bound_tokens` returns `Decimal::MAX`, this passes validation unconditionally.
3. After execution, `icrc1_total_supply` is now massively inflated.
4. Attacker submits a `TransferSnsTreasuryFunds` proposal. At validation time, `fetch_icps_per_sns_token` reads the inflated `current_supply`, computes a near-zero `icps_per_token`, which yields a near-zero XDR valuation of the treasury.
5. The treasury is classified as "small" → `NoLimit` → the full treasury balance is allowed to be transferred in a single 7-day window.
6. The `TransferSnsTreasuryFunds` proposal passes and drains the treasury.

### Impact Explanation

An attacker with sufficient SNS voting power can:
- Bypass the 7-day treasury transfer rate limit entirely by inflating the SNS token supply via `MintSnsTokens` (which has no enforced cap).
- Drain the entire SNS ICP or SNS token treasury in a single proposal window, rather than being limited to 25% or 300,000 XDR.

The `valuation` snapshot is taken at proposal submission time and stored in `ActionAuxiliary`, so the execution-time check also uses the manipulated valuation: [6](#0-5) 

This is a governance authorization / ledger conservation bug: the rate-limiting guard that is supposed to protect the SNS treasury can be fully neutralized by an on-chain action that is itself uncapped.

### Likelihood Explanation

- The attacker entry path is an unprivileged ingress call: any principal can submit SNS governance proposals (subject to having a neuron with sufficient stake/voting power).
- The `MintSnsTokens` unlimited minting is explicitly acknowledged in the codebase as a known gap (TODO NNS1-2982), meaning it is currently reachable in production.
- The manipulation requires only two sequential governance proposals and no off-chain infrastructure.
- The `MIN_XDRS_PER_ICP` floor only clamps the XDR/ICP rate, not `icps_per_token`; there is no analogous floor for the SNS token price, so the valuation can be driven arbitrarily close to zero. [7](#0-6) 

### Recommendation

1. **Immediately enforce the `MintSnsTokens` upper bound** by uncommenting the `TODO(NNS1-2982)` block in `recent_amount_total_upper_bound_tokens` for `MintSnsTokens`: [8](#0-7) 

2. **Add a floor on `icps_per_token`** analogous to `MIN_XDRS_PER_ICP` to prevent the computed SNS token price from being driven to near-zero by supply inflation.

3. **Use a time-weighted or snapshot-anchored supply** rather than the live `icrc1_total_supply` at proposal submission time, so that a supply spike within the same round cannot affect the valuation used for rate-limiting.

### Proof of Concept

```
// Step 1: Submit MintSnsTokens proposal for 10^15 SNS tokens (no cap enforced)
// -> passes because recent_amount_total_upper_bound_tokens returns Decimal::MAX

// Step 2: After execution, icrc1_total_supply is now ~10^15 * E8

// Step 3: fetch_icps_per_sns_token computes:
//   total_inflation = 10^15 * E8 / initial_supply_e8s  (e.g., 10^9)
//   = 10^6
//   current_icps_per_sns_token = initial_icps_per_sns_token / 10^6
//   ≈ 0.000001 ICP per SNS token

// Step 4: treasury XDR valuation = balance_tokens * 0.000001 * xdrs_per_icp
//   Even with 10,000 SNS tokens in treasury and ICP at 10 XDR:
//   = 10,000 * 0.000001 * 10 = 0.1 XDR  << 100,000 XDR threshold

// Step 5: ProposalsAmountTotalUpperBound::NoLimit -> full treasury balance allowed

// Step 6: TransferSnsTreasuryFunds for full treasury balance passes validation
```

The root cause is in `fetch_icps_per_sns_token` using live `icrc1_total_supply` as the inflation denominator, combined with `MintSnsTokens` having no enforced cap, making the supply directly attacker-controllable. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L314-334)
```rust
    async fn fetch_icps_per_sns_token(&self) -> Result<Decimal, ValuationError> {
        // (Concurrently) fetch the various pieces that we need to sythensize the result:
        let (get_derived_state_result, initial_supply_e8s_result, current_supply_result) = join!(
            // 1. SNS token price from swap.
            call::<_, MyRuntime>(self.swap_canister_id, GetDerivedStateRequest {}),
            // 2. Initial SNS token supply.
            initial_supply_e8s::<MyRuntime>(
                self.sns_token_ledger_canister_id,
                InitialSupplyOptions::new()
            ),
            // 3. Current SNS token supply.
            MyRuntime::call_with_cleanup::<_, (Nat,)>(
                self.sns_token_ledger_canister_id,
                "icrc1_total_supply",
                ()
            ),
        );
        // (Factors 2 and 3 tell us how much inflation there has been. For
        // example, if the amount of tokens has doubled since the beginning,
        // then the current ICPs per SNS token should be half of what it was at
        // the time of the swap.)
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L386-415)
```rust
        // Do actual (simple) math.

        // Flip the ratio from SNS tokens per ICP to ICPs per SNS token.
        let initial_icps_per_sns_token = Decimal::from(1)
            .checked_div(initial_sns_tokens_per_icp)
            .ok_or_else(|| {
            ValuationError::new_arithmetic(format!(
                "Unable to perform 1 / sns_tokens_per_icp (where sns_tokens_per_icp = {initial_sns_tokens_per_icp}).",
            ))
        })?;

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
    }
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L76-77)
```rust
        let result_tokens = match self_ {
            Self::NoLimit => balance_tokens,
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

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L137-140)
```rust
    fn clamp_xdrs_per_icp(valuation: &mut Valuation) {
        let xdrs_per_icp = &mut valuation.valuation_factors.xdrs_per_icp;
        *xdrs_per_icp = (*xdrs_per_icp).max(Self::MIN_XDRS_PER_ICP);
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
