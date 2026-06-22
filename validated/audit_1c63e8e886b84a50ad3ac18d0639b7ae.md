### Title
Inconsistent Multi-Canister State Reads in SNS Token Valuation Produce Manipulable Treasury Transfer Limits - (File: rs/sns/governance/token_valuation/src/lib.rs)

---

### Summary

`IcpsPerSnsTokenClient::fetch_icps_per_sns_token()` concurrently fetches three pieces of data from two different canisters — the SNS swap canister and the SNS ledger — without any atomicity guarantee. Because the IC execution model allows other messages to be processed between the time these inter-canister calls are dispatched and the time their responses are received, an attacker can manipulate the SNS ledger state (specifically `icrc1_total_supply`) between the concurrent reads. This produces an incorrect `icps_per_token` valuation, which is then used to enforce the 7-day treasury transfer limit for `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals.

---

### Finding Description

`fetch_icps_per_sns_token` in `rs/sns/governance/token_valuation/src/lib.rs` uses `futures::join!` to concurrently dispatch three inter-canister calls:

1. `get_derived_state` on the **swap canister** — returns `sns_tokens_per_icp` at genesis.
2. `initial_supply_e8s` on the **SNS ledger** — reads the first mint transaction to determine the initial token supply.
3. `icrc1_total_supply` on the **SNS ledger** — reads the current total token supply. [1](#0-0) 

The computed price is:

```
current_icps_per_token = (1 / initial_sns_tokens_per_icp) / (current_supply / initial_supply)
``` [2](#0-1) 

On the IC, `join!` dispatches all three calls in the same execution round, but responses arrive in subsequent rounds. Between dispatch and receipt, the IC scheduler processes other messages. An attacker who can submit ICRC-1 transactions to the SNS ledger (anyone can) can cause `icrc1_total_supply` to reflect a different state than `initial_supply_e8s`, producing a `total_inflation` ratio that does not correspond to any real point in time.

Additionally, `try_get_balance_valuation_factors` concurrently fetches the treasury's `icrc1_balance_of` alongside the `icps_per_token` computation, meaning the treasury balance and the price denominator are also read at potentially different instants. [3](#0-2) 

The resulting `Valuation` is passed directly into `treasury_valuation_if_proposal_amount_is_small_enough_or_err`, which computes the 7-day transfer allowance: [4](#0-3) 

The allowance is then used to gate both `TransferSnsTreasuryFunds` and `MintSnsTokens` proposals: [5](#0-4) [6](#0-5) 

The limit tiers are:

- Treasury < 100,000 XDR → **no limit** (full treasury can be transferred)
- Treasury 100,000–1,200,000 XDR → **25% of treasury per 7 days**
- Treasury > 1,200,000 XDR → **300,000 XDR per 7 days** [7](#0-6) 

---

### Impact Explanation

**Inflating the valuation (attacker burns tokens between reads):**
If `icrc1_total_supply` is read *after* a large burn, `total_inflation` is smaller than reality, so `icps_per_token` is inflated. A treasury that is genuinely in the "medium" tier (25% limit) can be pushed into the "large" tier (300,000 XDR absolute cap), or a "large" treasury can appear even larger, potentially allowing a single proposal to drain more than the intended 7-day cap.

**Deflating the valuation (attacker mints tokens between reads):**
If `icrc1_total_supply` is read *after* a large mint, `total_inflation` is larger than reality, so `icps_per_token` is deflated. A treasury that is genuinely in the "large" tier can be pushed into the "small" tier, where **no limit applies at all** — the entire treasury can be transferred in one proposal.

The `NoLimit` branch is the most dangerous outcome: [8](#0-7) 

---

### Likelihood Explanation

The attacker entry path requires only the ability to submit ICRC-1 transactions to the SNS ledger (permissionless) and to submit an SNS governance proposal (requires meeting the proposal submission threshold, which is typically a small neuron stake). The attacker does not need a governance majority, admin keys, or any privileged role. The timing window is multiple IC execution rounds (the duration of three concurrent inter-canister calls), which is on the order of seconds — wide enough to be reliably exploited by an automated script. The attack is repeatable: every time a `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal is submitted, the valuation is re-fetched, giving the attacker a fresh window.

---

### Recommendation

Replace the concurrent multi-canister reads with a design that either:

1. **Reads all supply data from a single atomic source**: fetch `icrc1_total_supply` and `icrc1_balance_of` in the same inter-canister call batch and verify they are consistent (e.g., by re-reading and comparing), or
2. **Uses a snapshot/certified read**: leverage the IC's certified variables mechanism so that `icrc1_total_supply` and `icrc1_balance_of` are read from the same certified state root, preventing mid-flight manipulation, or
3. **Adds a staleness/consistency check**: after receiving all responses, re-fetch `icrc1_total_supply` a second time and reject the valuation if it differs from the first read by more than a small tolerance, analogous to `ensureNotInVaultContext()` forcing a reentrancy check in the Balancer fix.

At minimum, `icrc1_balance_of` (treasury balance) and `icrc1_total_supply` should be fetched sequentially from the same canister in the same call, not concurrently with calls to a different canister, so that the supply ratio and the balance reflect the same ledger state.

---

### Proof of Concept

1. Attacker holds a small neuron stake sufficient to submit an SNS governance proposal.
2. Attacker submits a `TransferSnsTreasuryFunds` proposal requesting the full treasury balance.
3. SNS governance calls `assess_treasury_balance` → `try_get_sns_token_balance_valuation` → `fetch_icps_per_sns_token`, which dispatches three concurrent inter-canister calls.
4. While the calls are in-flight (between dispatch round and response round), the attacker submits a large ICRC-1 `icrc1_transfer` to mint/burn SNS tokens (if they control a minting account) or simply transfers a large amount to/from the treasury account to shift `icrc1_balance_of`.
5. The `icrc1_total_supply` response reflects the post-manipulation supply; `initial_supply_e8s` reflects the pre-manipulation ledger history. The computed `total_inflation` is incorrect.
6. If the manipulated valuation places the treasury below 100,000 XDR, `ProposalsAmountTotalUpperBound::NoLimit` is returned, and the full treasury balance is the allowed transfer amount — bypassing the intended 25% or 300,000 XDR cap.
7. The proposal passes validation and, once adopted by governance, executes the full treasury drain via `perform_transfer_sns_treasury_funds`. [9](#0-8)

### Citations

**File:** rs/sns/governance/token_valuation/src/lib.rs (L154-163)
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
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L316-330)
```rust
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

**File:** rs/sns/governance/src/proposal.rs (L551-578)
```rust
/// Validates and render TransferSnsTreasuryFunds proposal
///
/// Returns ActionAuxiliary::TransferSnsTreasuryFunds.
async fn validate_and_render_transfer_sns_treasury_funds(
    transfer: &TransferSnsTreasuryFunds,
    sns_transfer_fee_e8s: u64,
    env: &dyn Environment,
    swap_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
) -> Result<
    (
        String, // Rendering.
        ActionAuxiliary,
    ),
    String,
> {
    let mut defects = vec![];

    // Validate amount. This requires calling CMC and the swap canister; hence, await.
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        transfer,
    )
    .await;
```

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

**File:** rs/sns/governance/src/proposal.rs (L875-899)
```rust
async fn validate_and_render_mint_sns_tokens(
    mint_sns_tokens: &MintSnsTokens,
    sns_transfer_fee_e8s: u64,
    env: &dyn Environment,
    swap_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    proposals: impl Iterator<Item = &ProposalData>,
) -> Result<
    (
        String, // Rendering.
        ActionAuxiliary,
    ),
    String,
> {
    let mut defects = vec![];

    // Validate amount. (This requires calling CMC and the swap canister; hence, await.)
    let valuation = treasury_valuation_if_proposal_amount_is_small_enough_or_err(
        env,
        sns_ledger_canister_id,
        swap_canister_id,
        proposals,
        mint_sns_tokens,
    )
    .await;
```

**File:** rs/sns/governance/proposals_amount_total_limit/src/lib.rs (L34-135)
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

**File:** rs/sns/governance/src/governance.rs (L2980-3059)
```rust
    async fn perform_transfer_sns_treasury_funds(
        &mut self,
        proposal_id: u64, // This is just to control concurrency.
        valuation: Result<Valuation, GovernanceError>,
        transfer: &TransferSnsTreasuryFunds,
    ) -> Result<(), GovernanceError> {
        // Only execute one proposal of this type at a time.
        thread_local! {
            static IN_PROGRESS_PROPOSAL_ID: RefCell<Option<u64>> = const { RefCell::new(None) };
        }
        let release_on_drop = acquire(&IN_PROGRESS_PROPOSAL_ID, proposal_id);
        if let Err(already_in_progress_proposal_id) = release_on_drop {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Another TransferSnsTreasuryFunds proposal (ID = {already_in_progress_proposal_id}) is already in progress.",
                ),
            ));
        }

        transfer_sns_treasury_funds_amount_is_small_enough_at_execution_time_or_err(
            transfer,
            valuation?,
            self.proto.proposals.values(),
            self.env.now(),
        )?;

        let to = Account {
            owner: transfer
                .to_principal
                .expect("Expected transfer to have a target principal")
                .0,
            subaccount: transfer.to_subaccount.as_ref().map(|s| {
                bytes_to_subaccount(&s.subaccount[..])
                    .expect("Couldn't transform transfer.subaccount to Subaccount")
            }),
        };
        match transfer.from_treasury() {
            TransferFrom::IcpTreasury => self
                .nns_ledger
                .transfer_funds(
                    transfer.amount_e8s,
                    NNS_DEFAULT_TRANSFER_FEE.get_e8s(),
                    self.sns_treasury_icp_subaccount(),
                    to,
                    transfer.memo.unwrap_or(0),
                )
                .await
                .map(|_| ())
                .map_err(|e| {
                    GovernanceError::new_with_message(
                        ErrorType::External,
                        format!("Error making ICP treasury transfer: {e}"),
                    )
                }),
            TransferFrom::SnsTokenTreasury => {
                let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

                self.ledger
                    .transfer_funds(
                        transfer.amount_e8s,
                        transaction_fee_e8s,
                        self.sns_treasury_sns_token_subaccount(),
                        to,
                        transfer.memo.unwrap_or(0),
                    )
                    .await
                    .map(|_| ())
                    .map_err(|e| {
                        GovernanceError::new_with_message(
                            ErrorType::External,
                            format!("Error making SNS Token treasury transfer: {e}"),
                        )
                    })
            }
            TransferFrom::Unspecified => Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Invalid 'from_treasury' in transfer.",
            )),
        }
```
