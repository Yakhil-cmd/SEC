### Title
NNS Governance `split_neuron` Bypasses `voting_power_refreshed_timestamp_seconds` Decay Mechanism — (File: `rs/nns/governance/src/governance.rs`)

### Summary
The NNS governance `split_neuron` function creates a child neuron with `voting_power_refreshed_timestamp_seconds` set to the current time (`now()`), regardless of how stale the parent neuron's refresh timestamp is. A neuron controller whose neuron has accumulated significant voting-power decay (due to inactivity) can split it to produce a child neuron that starts with full deciding voting power, bypassing the decay penalty for the split portion — without performing any actual governance participation.

### Finding Description
The NNS governance system tracks `voting_power_refreshed_timestamp_seconds` per neuron. When this timestamp is more than ~6 months old, the neuron's `deciding_voting_power` begins to decrease linearly toward zero over the following month (`VotingPowerEconomics`). This mechanism is intended to reduce the governance influence of inactive neurons.

In `split_neuron`, the child neuron is constructed via `NeuronBuilder::new(child_nid, to_subaccount, *caller, parent_neuron.dissolve_state_and_age(), created_timestamp_seconds)`. The `created_timestamp_seconds` argument is `self.env.now()`, and the `NeuronBuilder` uses this as the initial `voting_power_refreshed_timestamp_seconds` for the child neuron. [1](#0-0) 

The test at line 5602–5616 explicitly confirms this: the child neuron's `voting_power_refreshed_timestamp_seconds` equals `driver.now()` and is strictly greater than the parent's timestamp. [2](#0-1) 

The parent's `voting_power_refreshed_timestamp_seconds` is left unchanged. The child neuron inherits the parent's `dissolve_state_and_age()` (including age and dissolve delay) but receives a brand-new refresh timestamp. [3](#0-2) 

### Impact Explanation
A neuron controller whose neuron has not voted or followed for 6–7 months (deciding VP approaching or at zero) can call `split_neuron` to carve off nearly all of the stake into a child neuron. The child neuron immediately has full `deciding_voting_power` equal to its `potential_voting_power`, because its `voting_power_refreshed_timestamp_seconds` is `now()`. The parent retains only the minimum stake and continues to have zero or near-zero deciding VP.

Concretely: a 1 000 ICP neuron with 0 deciding VP (7 months inactive) can be split into a 999 ICP child with full deciding VP and a 1 ICP parent with 0 deciding VP. The attacker recovers ~100% of their governance influence without casting a single vote or updating any following relationship. This directly undermines the NNS's mechanism for reducing the influence of passive/inactive large stakeholders, and could be exploited to swing close governance votes. [4](#0-3) [5](#0-4) 

### Likelihood Explanation
The `split_neuron` endpoint is publicly callable by any neuron controller via `manage_neuron` ingress messages. No privileged role is required. The operation costs only the ICP transaction fee. Any large-stake holder who has been inactive for 6+ months has a direct incentive to use this technique before a high-stakes proposal vote. The bypass is a predictable side-effect of the current child-neuron construction logic, not an obscure edge case. [6](#0-5) 

### Recommendation
When constructing the child neuron in `split_neuron`, set its `voting_power_refreshed_timestamp_seconds` to the **minimum** of `now()` and the parent's `voting_power_refreshed_timestamp_seconds`, rather than unconditionally using `now()`. This ensures that the decay penalty already accumulated by the parent is not erased for the split portion. Alternatively, document explicitly that `split_neuron` is an intentional refresh path and evaluate whether the `VotingPowerEconomics` decay mechanism provides meaningful protection given this escape hatch.

### Proof of Concept
1. Neuron A holds 1 000 ICP, `voting_power_refreshed_timestamp_seconds` = 7 months ago → `deciding_voting_power` = 0.
2. Controller calls `manage_neuron` → `Split { amount_e8s: 999_ICP + tx_fee, memo: None }`.
3. `split_neuron` creates child neuron B with `voting_power_refreshed_timestamp_seconds = now()`.
4. Child neuron B immediately has `deciding_voting_power` = `potential_voting_power` for 999 ICP (full age and dissolve-delay bonuses inherited from parent).
5. Parent neuron A retains 1 ICP with `deciding_voting_power` = 0.
6. Controller now controls ~999 ICP of full deciding VP and can vote on any open proposal, bypassing the inactivity penalty entirely. [1](#0-0) [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2113-2148)
```rust
    /// Splits a neuron into two neurons.
    ///
    /// The parent neuron's stake is decreased by the amount specified in
    /// Split, while the child neuron is created with a stake
    /// equal to that amount, minus the transfer fee.
    ///
    /// The child neuron inherits all the properties of its parent
    /// including age and dissolve state.
    ///
    /// On success returns the newly created neuron's id.
    ///
    /// Preconditions:
    /// - The parent neuron exists
    /// - The caller is the controller of the neuron.
    /// - The parent neuron is not already undergoing ledger updates.
    /// - The parent neuron is not spawning.
    /// - The staked amount minus amount to split is more than the minimum
    ///   stake.
    /// - The amount to split minus the transfer fee is more than the minimum
    ///   stake.
    #[cfg_attr(feature = "tla", tla_update_method(SPLIT_NEURON_DESC.clone(), tla_snapshotter!()))]
    pub async fn split_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        split: &manage_neuron::Split,
    ) -> Result<NeuronId, GovernanceError> {
        // New neurons are not allowed when the heap is too large.
        self.check_heap_can_grow()?;

        let neuron_limit_reservation = self.rate_limiter.try_reserve(
            self.env.now_system_time(),
            NEURON_RATE_LIMITER_KEY.to_string(),
            1,
        )?;

```

**File:** rs/nns/governance/src/governance.rs (L2241-2257)
```rust
        let child_neuron = NeuronBuilder::new(
            child_nid,
            to_subaccount,
            *caller,
            parent_neuron.dissolve_state_and_age(),
            created_timestamp_seconds,
        )
        .with_hot_keys(parent_neuron.hot_keys.clone())
        .with_followees(parent_neuron.followees.clone())
        .with_kyc_verified(parent_neuron.kyc_verified)
        .with_auto_stake_maturity(parent_neuron.auto_stake_maturity.unwrap_or(false))
        .with_not_for_profit(parent_neuron.not_for_profit)
        .with_joined_community_fund_timestamp_seconds(
            parent_neuron.joined_community_fund_timestamp_seconds,
        )
        .with_neuron_type(parent_neuron.neuron_type)
        .build();
```

**File:** rs/nns/governance/tests/governance.rs (L5547-5616)
```rust
    let child_nid = governance
        .split_neuron(
            &id,
            &from,
            &Split {
                amount_e8s: 200_000_000,
                memo: None,
            },
        )
        .now_or_never()
        .unwrap()
        .unwrap();

    // We should now have 2 neurons.
    assert_eq!(governance.neuron_store.len(), 2);
    // And we should have two ledger accounts.
    driver.assert_num_neuron_accounts_exist(2);

    let child_neuron = governance
        .get_full_neuron(&child_nid, &from)
        .expect("The child neuron is missing");
    let parent_neuron = governance
        .get_full_neuron(&id, &from)
        .expect("The parent neuron is missing");

    assert_eq!(
        parent_neuron.cached_neuron_stake_e8s,
        neuron_stake_e8s - 200_000_000
    );
    assert_eq!(parent_neuron.maturity_e8s_equivalent, 400_000_000);
    assert_eq!(
        parent_neuron.staked_maturity_e8s_equivalent,
        Some(320_000_000)
    );
    assert_eq!(child_neuron.controller, parent_neuron.controller);
    assert_eq!(
        child_neuron.cached_neuron_stake_e8s,
        200_000_000 - transaction_fee
    );
    assert_eq!(child_neuron.maturity_e8s_equivalent, 100_000_000);
    assert_eq!(
        child_neuron.staked_maturity_e8s_equivalent,
        Some(80_000_000)
    );
    assert_eq!(child_neuron.created_timestamp_seconds, driver.now(),);
    assert_ne!(
        child_neuron.created_timestamp_seconds,
        parent_neuron.created_timestamp_seconds,
    );
    assert_eq!(
        child_neuron.aging_since_timestamp_seconds,
        parent_neuron.aging_since_timestamp_seconds
    );
    assert_eq!(child_neuron.dissolve_state, parent_neuron.dissolve_state);
    assert_eq!(child_neuron.kyc_verified, true);
    assert_eq!(
        child_neuron.voting_power_refreshed_timestamp_seconds,
        Some(driver.now()),
    );
    assert!(
        child_neuron
            .voting_power_refreshed_timestamp_seconds
            .unwrap()
            > parent_neuron
                .voting_power_refreshed_timestamp_seconds
                .unwrap(),
        "{:?} vs. {:?}",
        child_neuron.voting_power_refreshed_timestamp_seconds,
        parent_neuron.voting_power_refreshed_timestamp_seconds,
    );
```

**File:** rs/nns/governance/src/neuron/types.rs (L371-399)
```rust
    pub fn potential_and_deciding_voting_power(
        &self,
        voting_power_economics: &VotingPowerEconomics,
        now_seconds: u64,
    ) -> (u64, u64) {
        let stake_e8s = self.stake_e8s();
        let boost = dissolve_delay_bonus_multiplier(self.dissolve_delay_seconds(now_seconds))
            * age_bonus_multiplier(self.age_seconds(now_seconds));
        let mut potential_voting_power = Decimal::from(stake_e8s) * boost;

        // 8 Year Gang bonus. Cap the bonus base to the current stake because
        // rejection fees can cause the bonus base to exceed stake_e8s.
        if is_mission_70_voting_rewards_enabled() {
            let eight_year_gang_bonus_base_e8s = self.eight_year_gang_bonus_base_e8s.min(stake_e8s);
            potential_voting_power +=
                Decimal::from(eight_year_gang_bonus_base_e8s) / Decimal::from(10) * boost;
        }

        // For DECIDING voting power.
        let adjustment_factor: Decimal = {
            let time_since_last_refreshed = Duration::from_secs(
                now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
            );

            voting_power_economics
                .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
        };

        let deciding_voting_power = adjustment_factor * potential_voting_power.floor();
```
