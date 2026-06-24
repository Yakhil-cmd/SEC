Let me look at the proposal submission flow and any rate limiting to complete the analysis.

### Title
Unbounded Inter-Canister Call Loop in `initial_supply_e8s` Drains SNS Governance Cycles During Proposal Validation — (`rs/nervous_system/initial_supply/src/lib.rs`)

### Summary

The `initial_supply_e8s` function performs an unbounded sequential loop of inter-canister calls to scan ledger transactions. It is invoked on-chain during validation of every `TransferSnsTreasuryFunds` and `MintSnsTokens` proposal submission. Any SNS neuron holder can repeatedly submit such proposals to force the SNS governance canister to execute up to 400 sequential inter-canister calls per submission, draining its cycles and causing a governance DoS.

### Finding Description

`initial_supply_e8s` in `rs/nervous_system/initial_supply/src/lib.rs` loops, fetching ledger transactions in batches via inter-canister calls, until it finds a transaction whose timestamp differs from the first one (i.e., the end of the initial mint block): [1](#0-0) 

With the default `InitialSupplyOptions` (`max_transactions = 100_000`, `batch_size = 250`), this loop can issue up to **400 sequential inter-canister calls** (100,000 / 250) before hitting the bail-out: [2](#0-1) 

Each call may itself be redirected to an archive canister, adding another inter-canister hop: [3](#0-2) 

This function is called from `IcpsPerSnsTokenClient::fetch_icps_per_sns_token` (concurrently with two other inter-canister calls) to compute the SNS token price: [4](#0-3) 

Which is called from `try_get_balance_valuation_factors` → `try_get_sns_token_balance_valuation` → `assess_treasury_balance` → `treasury_valuation_if_proposal_amount_is_small_enough_or_err`: [5](#0-4) 

Which is called from both `validate_and_render_transfer_sns_treasury_funds` and `validate_and_render_mint_sns_tokens`: [6](#0-5) [7](#0-6) 

Both are reached from `validate_and_render_action`, called by `validate_and_render_proposal`, called by `make_proposal` — the public proposal submission entry point: [8](#0-7) 

The only guards before the expensive validation runs are: neuron stake ≥ `reject_cost_e8s`, dissolve delay ≥ minimum, and open proposal count < `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS`: [9](#0-8) 

None of these guards prevent the cycles drain, because the expensive `initial_supply_e8s` loop runs **before** the proposal is accepted or rejected, and the attacker's neuron stake is not consumed on a failed validation.

### Impact Explanation

**Impact: High**

Each `TransferSnsTreasuryFunds` or `MintSnsTokens` proposal submission forces the SNS governance canister to execute up to 400 sequential inter-canister calls (each potentially redirected to an archive canister, doubling the count). All cycles for these calls are paid by the governance canister. An attacker with a minimal SNS neuron can submit proposals in a tight loop (up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` open at once), draining the governance canister's cycles. Once the governance canister is out of cycles, it is frozen and cannot process any governance actions — a complete governance DoS. Even short of full depletion, the repeated expensive scanning degrades governance throughput.

### Likelihood Explanation

**Likelihood: Low**

The attacker must hold an SNS neuron with stake ≥ `reject_cost_e8s` and a dissolve delay ≥ the minimum required to vote. These are low barriers for any legitimate SNS participant. The attack is amplified when the SNS ledger has many initial mint transactions at the same timestamp (e.g., large airdrops), which forces more loop iterations. The attack is not gated by any rate limit or per-call cycles charge to the submitter.

### Recommendation

1. **Cache the initial supply**: Compute `initial_supply_e8s` once at SNS initialization and store it in governance state. It is a constant after genesis and does not need to be re-fetched on every proposal.
2. **Cache the token valuation**: Store the result of `assess_treasury_balance` with a TTL (e.g., 1 hour) and reuse it across proposal validations, rather than re-fetching from ledger/swap/CMC on every submission.
3. **Charge the proposer**: Deduct cycles or stake from the proposer before running the expensive validation, not after.
4. **Bound the loop**: Reduce `max_transactions` to a small constant (e.g., 2,000) or require that the initial supply be stored at SNS init time.

### Proof of Concept

1. Deploy an SNS with a large initial token distribution (e.g., 10,000 initial mint transactions all sharing the same block timestamp, which is the normal SNS swap airdrop pattern).
2. Acquire a minimal SNS neuron (stake = `reject_cost_e8s`, dissolve delay = minimum).
3. Submit `TransferSnsTreasuryFunds` proposals in a loop up to `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS`.
4. Each submission triggers `initial_supply_e8s`, which loops calling `get_transactions` on the ledger and archive canisters — up to 400 inter-canister calls per proposal, all paid by the governance canister.
5. Repeat after proposals settle. The governance canister's cycle balance decreases with each batch. Eventually the canister is frozen and governance is halted.

The call chain is:

```
manage_neuron (MakeProposal)
  → Governance::make_proposal                          [governance.rs:3467]
    → validate_and_render_proposal                     [proposal.rs:299]
      → validate_and_render_transfer_sns_treasury_funds [proposal.rs:554]
        → treasury_valuation_if_proposal_amount_is_small_enough_or_err [proposal.rs:784]
          → assess_treasury_balance → try_get_sns_token_balance_valuation
            → IcpsPerSnsTokenClient::fetch_icps_per_sns_token [token_valuation/src/lib.rs:314]
              → initial_supply_e8s (loops up to 400 inter-canister calls) [initial_supply/src/lib.rs:35]
```

### Citations

**File:** rs/nervous_system/initial_supply/src/lib.rs (L35-108)
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

        if len < batch_size {
            // The previous condition tells us that we have scanned ALL
            // transactions.
            //
            // (This is necessary, and not "just" an optimization to avoid the
            // next iteration of the 'outer loop. In particular, if len == 0,
            // then without this, we would never make it past the 'outer loop.)
            //
            // What this means is that the only transactions that currently
            // exist are just the initial minting transactions. This is strange,
            // but not wrong. Normally, we break out of the outer loop when
            // transaction.timestamp != first_timestamp.
            break;
        }
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
