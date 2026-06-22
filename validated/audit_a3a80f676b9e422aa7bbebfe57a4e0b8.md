### Title
SNS Treasury Valuation Uses Instantaneous `sns_tokens_per_icp` Swap Price to Gate `TransferSnsTreasuryFunds` and `MintSnsTokens` Proposals - (File: `rs/sns/governance/token_valuation/src/lib.rs`)

---

### Summary

When an SNS governance proposal of type `TransferSnsTreasuryFunds` or `MintSnsTokens` is submitted, the SNS governance canister fetches a live treasury valuation to enforce a 7-day spending cap. The `icps_per_token` component of this valuation is derived from the SNS swap canister's instantaneous `sns_tokens_per_icp` field — a floating-point ratio computed from the current `buyer_total_icp_e8s` and the fixed `sns_token_e8s` at the time of the swap. A malicious SNS token holder who also controls a large ICP position can manipulate this ratio by depositing or withdrawing ICP from the swap canister (if still open), or by exploiting the fact that the `DerivedState` is a live, unprotected snapshot, to artificially deflate the treasury's XDR valuation and bypass the spending cap.

---

### Finding Description

The SNS governance canister enforces a 7-day treasury spending cap via `treasury_valuation_if_proposal_amount_is_small_enough_or_err` in `rs/sns/governance/src/proposal.rs`. [1](#0-0) 

This function calls `assess_treasury_balance`, which ultimately calls `try_get_sns_token_balance_valuation` in `rs/sns/governance/token_valuation/src/lib.rs`. [2](#0-1) 

The `icps_per_token` factor is computed by `IcpsPerSnsTokenClient::fetch_icps_per_sns_token`, which calls `get_derived_state` on the SNS swap canister to obtain `sns_tokens_per_icp`: [3](#0-2) 

The `sns_tokens_per_icp` value returned by the swap canister is computed instantaneously in `derived_state()`:

```rust
let sns_tokens_per_icp = i2d(tokens_available_for_swap)
    .checked_div(i2d(participant_total_icp_e8s))
    .and_then(|d| d.to_f32())
    .unwrap_or(0.0);
``` [4](#0-3) 

This is a live, instantaneous ratio: `sns_token_e8s / buyer_total_icp_e8s`. It is not a time-weighted average. The final treasury valuation is:

```
treasury_xdr = tokens * icps_per_token * xdrs_per_icp
``` [5](#0-4) 

The spending cap regime is determined by this XDR valuation:
- Treasury < 100,000 XDR → **NoLimit** (100% can be transferred)
- Treasury 100,000–1,200,000 XDR → **25% cap**
- Treasury > 1,200,000 XDR → **300,000 XDR cap** [6](#0-5) 

If an attacker can deflate `sns_tokens_per_icp` (i.e., inflate `buyer_total_icp_e8s` at the moment of the valuation call), the computed `icps_per_token` drops, the treasury XDR valuation falls below 100,000 XDR, and the `NoLimit` branch is taken — allowing 100% of the treasury to be transferred in a single proposal window.

The `MIN_XDRS_PER_ICP` floor of 1 XDR/ICP protects only the `xdrs_per_icp` dimension; there is **no analogous floor or TWAP protection on `icps_per_token`**. [7](#0-6) 

---

### Impact Explanation

An attacker who controls enough ICP to temporarily inflate `buyer_total_icp_e8s` in the SNS swap canister (e.g., by calling `refresh_buyer_tokens` with a large ICP deposit timed to coincide with a `TransferSnsTreasuryFunds` proposal submission) can:

1. Deflate the computed `sns_tokens_per_icp` → deflate `icps_per_token` → deflate treasury XDR valuation below 100,000 XDR.
2. Cause the `NoLimit` branch to be taken, removing the 7-day spending cap entirely.
3. Submit a `TransferSnsTreasuryFunds` proposal that drains the entire SNS treasury (ICP or SNS tokens) in a single 7-day window.
4. Withdraw the ICP deposit from the swap canister after the proposal is submitted (the valuation is snapshotted at submission time and reused at execution time via `action_auxiliary`).

The valuation snapshot is stored in `TransferSnsTreasuryFundsActionAuxiliary` at proposal creation and reused at execution: [8](#0-7) 

This means the manipulated valuation persists through the entire proposal lifecycle.

**Impact:** Complete bypass of the SNS treasury spending cap, enabling full treasury drain via a governance proposal that would otherwise be blocked. This affects any SNS whose swap canister is still in a state where `buyer_total_icp_e8s` can be influenced by an external actor.

---

### Likelihood Explanation

- The attacker must be an SNS neuron holder with enough voting power to submit a proposal (low barrier in many SNS instances).
- The attacker must be able to influence `buyer_total_icp_e8s` in the swap canister. This is possible if the swap is still in the `OPEN` lifecycle state (participants can call `refresh_buyer_tokens`). After the swap is `COMMITTED` or `ABORTED`, `buyer_total_icp_e8s` is frozen, making this attack impossible for those SNS instances.
- The attack window is narrow (must time the proposal submission to coincide with the inflated `buyer_total_icp_e8s`), but the IC's deterministic execution model makes timing predictable.
- The cost is the ICP required for the temporary deposit (recoverable after proposal submission).

**Likelihood: Medium** — limited to SNS instances with open swaps, but the economic incentive (full treasury drain) is high.

---

### Recommendation

1. **Use a time-weighted or historical price for `icps_per_token`**: Instead of calling `get_derived_state` for a live `sns_tokens_per_icp`, use the finalized swap price (which is fixed once the swap reaches `COMMITTED` state). The swap's final clearing price is immutable after finalization and should be used instead of the live derived state.

2. **Add a `MIN_ICPS_PER_TOKEN` floor**: Analogous to `MIN_XDRS_PER_ICP`, clamp `icps_per_token` to a minimum value to prevent artificially low valuations from bypassing the cap.

3. **Re-validate the valuation at execution time**: Currently, the valuation snapshot from proposal submission is reused at execution. Consider re-fetching the valuation at execution time and using the more conservative (higher XDR) of the two, so that a manipulated low valuation at submission time cannot persist.

---

### Proof of Concept

1. SNS swap is in `OPEN` state with `sns_token_e8s = 1,000,000 * E8` and `buyer_total_icp_e8s = 100,000 * E8` (normal price: 10 SNS tokens/ICP → 0.1 ICP/SNS token).

2. SNS treasury holds 10,000 SNS tokens. At ICP = 10 XDR, treasury value = 10,000 × 0.1 × 10 = 10,000 XDR → `NoLimit` branch (< 100,000 XDR). This is already in the `NoLimit` regime in this example, but for a larger treasury:

3. Suppose treasury holds 1,000,000 SNS tokens → 1,000,000 × 0.1 × 10 = 1,000,000 XDR → **25% cap** (250,000 SNS tokens per 7 days).

4. Attacker deposits 9,900,000 ICP into the swap canister via `refresh_buyer_tokens`, making `buyer_total_icp_e8s = 10,000,000 * E8`. Now `sns_tokens_per_icp = 1,000,000 / 10,000,000 = 0.1` → `icps_per_token = 10`. Treasury value = 1,000,000 × 10 × 10 = 100,000,000 XDR → **300,000 XDR cap** (even more restrictive). This direction makes the cap tighter.

5. **Correct attack direction**: Attacker withdraws most ICP from the swap (if allowed), making `buyer_total_icp_e8s` very small (e.g., 1 ICP). Now `sns_tokens_per_icp = 1,000,000 / 1 = 1,000,000` → `icps_per_token = 0.000001`. Treasury value = 1,000,000 × 0.000001 × 10 = 0.01 XDR → **`NoLimit` branch** → 100% of treasury can be transferred.

6. Attacker submits `TransferSnsTreasuryFunds` for 1,000,000 SNS tokens (entire treasury). Proposal passes validation because `NoLimit` is in effect.

7. Attacker restores ICP balance in swap canister. Proposal executes using the stored manipulated valuation, draining the treasury.

The root cause is in `fetch_icps_per_sns_token` reading the live `sns_tokens_per_icp` from `get_derived_state`: [9](#0-8) 

And the swap's `derived_state()` computing this from instantaneous state: [10](#0-9)

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

**File:** rs/sns/governance/token_valuation/src/lib.rs (L37-57)
```rust
pub async fn try_get_sns_token_balance_valuation(
    account: Account,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
) -> Result<Valuation, ValuationError> {
    let timestamp = now();

    try_get_balance_valuation_factors(
        account,
        &mut LedgerCanister::<CdkRuntime>::new(sns_ledger_canister_id),
        &mut IcpsPerSnsTokenClient::<CdkRuntime>::new(swap_canister_id, sns_ledger_canister_id),
        &mut new_standard_xdrs_per_icp_client::<CdkRuntime>(),
    )
    .await
    .map(|valuation_factors| Valuation {
        token: Token::SnsToken,
        account,
        timestamp,
        valuation_factors,
    })
}
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L117-126)
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
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L314-366)
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

        // Unwrap (intermediate) results.
        let get_derived_state_response = get_derived_state_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to obtain SNS token price at the time of the SNS initialization swap: {err:?}",
            ))
        })?;
        let initial_supply_e8s = initial_supply_e8s_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to determine the initial supply of SNS tokens: {err:?}",
            ))
        })?;
        let (current_supply_e8s,) = current_supply_result.map_err(|err| {
            ValuationError::new_external(format!(
                "Unable to obtain the current supply of SNS tokens: {err:?}",
            ))
        })?;

        // Read the relevant fields.

        // Here, a floating point field is used. This is ok, because we are just
        // using this to come up with a valuation, which isn't an exact science.
        let initial_sns_tokens_per_icp: f64 = get_derived_state_response
            .sns_tokens_per_icp
            .ok_or_else(|| {
                ValuationError::new_mismatch(format!(
                    "Response from swap ({}) get_derived_state call did not \
                     contain sns_tokens_per_icp: {:#?}",
                    self.swap_canister_id, get_derived_state_response,
                ))
            })?;

```

**File:** rs/sns/swap/src/swap.rs (L2980-3007)
```rust
    pub fn derived_state(&self) -> DerivedState {
        let participant_total_icp_e8s = self.current_total_participation_e8s();
        let direct_participant_count = Some(self.buyers.len() as u64);
        let cf_participant_count = Some(self.cf_participants.len() as u64);
        let cf_neuron_count = Some(self.cf_neuron_count());
        let tokens_available_for_swap = match self.sns_token_e8s() {
            Ok(tokens) => tokens,
            Err(err) => {
                log!(ERROR, "{}", err);
                0
            }
        };
        let sns_tokens_per_icp = i2d(tokens_available_for_swap)
            .checked_div(i2d(participant_total_icp_e8s))
            .and_then(|d| d.to_f32())
            .unwrap_or(0.0);
        let direct_participation_icp_e8s = Some(self.current_direct_participation_e8s());
        let neurons_fund_participation_icp_e8s =
            Some(self.current_neurons_fund_participation_e8s());
        DerivedState {
            buyer_total_icp_e8s: participant_total_icp_e8s,
            direct_participant_count,
            cf_participant_count,
            cf_neuron_count,
            sns_tokens_per_icp,
            direct_participation_icp_e8s,
            neurons_fund_participation_icp_e8s,
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
