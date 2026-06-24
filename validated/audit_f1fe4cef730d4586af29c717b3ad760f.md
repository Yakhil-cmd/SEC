Audit Report

## Title
Unbounded Inter-Canister Call Loop in `initial_supply_e8s` Drains SNS Governance Cycles During Proposal Validation — (`rs/nervous_system/initial_supply/src/lib.rs`)

## Summary

The `initial_supply_e8s` function in `rs/nervous_system/initial_supply/src/lib.rs` executes an unbounded sequential loop of inter-canister calls during SNS proposal validation. Because `validate_and_render_proposal` is invoked at the very start of `make_proposal` — before any neuron stake, dissolve delay, or proposal-count guards — any SNS neuron holder meeting the minimum stake and dissolve delay requirements can repeatedly trigger up to 400 sequential inter-canister calls per `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal submission, with all cycle costs borne by the SNS governance canister. Sustained abuse can deplete the governance canister's cycles, causing a complete governance DoS.

## Finding Description

**Root cause — unbounded loop in `initial_supply_e8s`:**

The outer loop in `rs/nervous_system/initial_supply/src/lib.rs` (lines 35–108) fetches ledger transactions in batches via `get_transactions` until it encounters a transaction whose timestamp differs from the first one. With the default `InitialSupplyOptions` (`max_transactions = 100_000`, `batch_size = 250`), the loop can execute up to **400 sequential inter-canister calls** before the bail-out at line 62 fires. [1](#0-0) [2](#0-1) 

Each `get_transactions` call may itself redirect to an archive canister, adding a second inter-canister hop per batch iteration. [3](#0-2) 

**Call chain to proposal submission:**

`initial_supply_e8s` is called inside `IcpsPerSnsTokenClient::fetch_icps_per_sns_token` (concurrently with two other calls via `join!`, but the loop itself is sequential): [4](#0-3) 

This feeds into `try_get_balance_valuation_factors` → `try_get_sns_token_balance_valuation` → `assess_treasury_balance` → `treasury_valuation_if_proposal_amount_is_small_enough_or_err`: [5](#0-4) [6](#0-5) 

Which is called from both `validate_and_render_transfer_sns_treasury_funds` and `validate_and_render_mint_sns_tokens`: [7](#0-6) [8](#0-7) 

**Critical ordering flaw — validation runs before all guards:**

In `make_proposal`, `validate_and_render_proposal` (which triggers the entire expensive call chain) is invoked at line 3467, **before** the neuron stake check (line 3521), dissolve delay check (line 3510), and open-proposal-count check (lines 3532–3547): [9](#0-8) [10](#0-9) 

This means the 400-call loop executes and its cycle cost is paid by the governance canister regardless of whether the proposal ultimately passes or fails validation, and regardless of whether the proposer's stake is sufficient. The `reject_cost_e8s` is only deducted from the proposer's neuron after the proposal is accepted (line 3644+), not before the expensive validation. [11](#0-10) 

## Impact Explanation

**Impact: High** — Application/platform-level DoS of SNS governance.

An attacker with a minimal SNS neuron (stake ≥ `reject_cost_e8s`, dissolve delay ≥ minimum) can submit up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` (700) `TransferSnsTreasuryFunds` or `MintSnsTokens` proposals in rapid succession. Each submission forces the SNS governance canister to execute up to 400 sequential inter-canister calls (potentially 800 with archive redirects), all paid from the governance canister's cycle balance. Once the governance canister's cycles are exhausted, it is frozen and all governance actions — including upgrades, treasury transfers, and parameter changes — are halted. This is a concrete, non-hypothetical governance DoS matching the allowed impact class: "Application/platform-level DoS... not based on raw volumetric DDoS."

## Likelihood Explanation

**Likelihood: Low-to-Medium.** The attacker must hold an SNS neuron with stake ≥ `reject_cost_e8s` and a dissolve delay ≥ the minimum required to vote — low barriers for any legitimate SNS participant. The attack is amplified when the SNS ledger has many initial mint transactions sharing the same block timestamp (the normal SNS swap airdrop pattern), which forces more loop iterations per submission. The attacker pays `reject_cost_e8s` per rejected proposal in SNS tokens, but the cycles drain is asymmetric: the governance canister pays cycles while the attacker pays tokens. No per-call cycles charge is imposed on the submitter, and no rate limit exists on proposal submission beyond the open-proposal cap.

## Recommendation

1. **Cache the initial supply at SNS initialization.** `initial_supply_e8s` is a constant after genesis. Compute it once during SNS init and store it in governance state; eliminate the per-proposal re-scan entirely.
2. **Cache the treasury valuation with a TTL.** Store the result of `assess_treasury_balance` (e.g., with a 1-hour TTL) and reuse it across proposal validations rather than re-fetching from ledger/swap/CMC on every submission.
3. **Move neuron eligibility checks before expensive validation.** Reorder `make_proposal` so that the neuron stake check, dissolve delay check, and proposal-count check all execute before `validate_and_render_proposal` is called.
4. **Reduce `max_transactions`.** Lower the default `max_transactions` in `InitialSupplyOptions` from 100,000 to a small constant (e.g., 2,000), or require the initial supply to be stored at SNS init time.

## Proof of Concept

1. Deploy an SNS with a large initial token distribution (e.g., 10,000 initial mint transactions all sharing the same block timestamp — the normal SNS swap airdrop pattern).
2. Acquire a minimal SNS neuron (stake = `reject_cost_e8s`, dissolve delay = minimum required).
3. In a loop, submit `TransferSnsTreasuryFunds` proposals with a small valid amount (below the 7-day limit) so that validation succeeds and the proposal is accepted into the open state.
4. Observe that each submission triggers `initial_supply_e8s`, which loops calling `get_transactions` on the ledger and archive canisters — up to 400 inter-canister calls per proposal, all paid by the governance canister.
5. Continue until `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` (700) open proposals exist; wait for them to settle; repeat.
6. Monitor the governance canister's cycle balance: it decreases with each batch. Eventually the canister is frozen and governance is halted.

A deterministic integration test using PocketIC can reproduce this by: (a) setting up an SNS with many same-timestamp mint transactions, (b) submitting proposals in a loop while mocking the ledger to return the maximum number of same-timestamp transactions per batch, and (c) asserting that the governance canister's cycle balance decreases proportionally to the number of proposals submitted.

### Citations

**File:** rs/nervous_system/initial_supply/src/lib.rs (L35-66)
```rust
    'outer: loop {
        let transactions = ledger
            .get_transactions::<MyRuntime>(transaction_count, batch_size)
            .await?;

        // This will be used later to determine whether we can break early.
        let len = transactions.len();
        let len = u64::try_from(len).map_err(|err| {
            format!("Unable to convert transactions length ({len}) to a u64: {err:?}",)
        })?;

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
```

**File:** rs/nervous_system/initial_supply/src/lib.rs (L132-139)
```rust
impl InitialSupplyOptions {
    /// Sensible values.
    pub fn new() -> Self {
        Self {
            max_transactions: 100_000,
            batch_size: 250,
        }
    }
```

**File:** rs/nervous_system/initial_supply/src/lib.rs (L186-204)
```rust
        let mut result = vec![];
        // Fetch transactions from archive.
        for archived_range in response.archived_transactions {
            let mut transactions = self
                .follow_get_transactions_redirect::<MyRuntime>(archived_range)
                .await
                .map_err(|err| {
                    format!(
                        "Ledger {} (partially) forwarded us (presumably to archive), \
                         but that failed: {}",
                        self.ledger_canister_id, err,
                    )
                })?;
            result.append(&mut transactions);
        }

        result.append(&mut response.transactions);

        Ok(result)
```

**File:** rs/sns/governance/token_valuation/src/lib.rs (L314-330)
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
```

**File:** rs/sns/governance/src/treasury.rs (L256-270)
```rust
pub(crate) async fn assess_treasury_balance(
    token: Token,
    sns_governance_canister_id: CanisterId,
    sns_ledger_canister_id: CanisterId,
    swap_canister_id: CanisterId,
) -> Result<Valuation, String> {
    let treasury_account = token.treasury_account(sns_governance_canister_id)?;
    let valuation = token
        .assess_balance(sns_ledger_canister_id, swap_canister_id, treasury_account)
        .await
        .map_err(|valuation_error| {
            format!("Unable to assess current treasury balance: {valuation_error:?}")
        })?;
    Ok(valuation)
}
```

**File:** rs/sns/governance/src/proposal.rs (L554-578)
```rust
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

**File:** rs/sns/governance/src/proposal.rs (L770-790)
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

**File:** rs/sns/governance/src/governance.rs (L3457-3467)
```rust
    pub async fn make_proposal(
        &mut self,
        proposer_id: &NeuronId,
        caller: &PrincipalId,
        proposal: &Proposal,
    ) -> Result<ProposalId, GovernanceError> {
        let now_seconds = self.env.now();

        // Validate proposal
        // TODO: return the optional extension spec
        let (rendering, action_auxiliary) = self.validate_and_render_proposal(proposal).await?;
```

**File:** rs/sns/governance/src/governance.rs (L3519-3547)
```rust
        // If the current stake of the proposer neuron is less than the cost
        // of having a proposal rejected, the neuron cannot make a proposal.
        if proposer.stake_e8s() < reject_cost_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Neuron doesn't have enough stake to submit proposal.",
            ));
        }

        // Check that there are not too many proposals.  What matters
        // here is the number of proposals for which ballots have not
        // yet been cleared, because ballots take the most amount of
        // space.
        if self
            .proto
            .proposals
            .values()
            .filter(|data| !data.ballots.is_empty())
            .count()
            >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
            && !proposal.allowed_when_resources_are_low()
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                "Reached maximum number of proposals that have not yet \
                been taken into account for voting rewards. \
                Please try again later.",
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L3644-3660)
```rust
        // Charge the cost of rejection upfront.
        // This will protect from DoS in couple of ways:
        // - It prevents a neuron from having too many proposals outstanding.
        // - It reduces the voting power of the submitter so that for every proposal
        //   outstanding the submitter will have less voting power to get it approved.
        self.proto
            .neurons
            .get_mut(&proposer_id.to_string())
            .expect("Proposer not found.")
            .neuron_fees_e8s += proposal_data.reject_cost_e8s;

        let function_id = u64::from(action);

        // Cast a 'yes'-vote for the proposer, including following.
        Governance::cast_vote_and_cascade_follow(
            &proposal_id,
            proposer_id,
```
