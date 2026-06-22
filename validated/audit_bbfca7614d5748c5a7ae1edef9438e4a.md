### Title
`NeuronInfo.voting_power` Query Field Returns `potential_voting_power` Instead of `deciding_voting_power`, Misleading Callers About Actual Voting Influence - (File: `rs/nns/governance/src/neuron/types.rs`)

---

### Summary

The `voting_power` field in `NeuronInfo`, returned by the `get_neuron_info` and `list_neurons` query endpoints of the NNS Governance canister, is populated with `potential_voting_power` instead of `deciding_voting_power`. The field's own documentation comment explicitly states it "has the same value as deciding_voting_power," making this a direct code-vs-comment inconsistency. For neurons that have not refreshed their voting power recently (stale neurons), `deciding_voting_power` is strictly less than `potential_voting_power` — meaning any caller reading the deprecated `voting_power` field will observe an inflated value that does not reflect the neuron's actual influence in governance.

---

### Finding Description

In `rs/nns/governance/src/neuron/types.rs`, the `get_neuron_info()` method constructs a `NeuronInfo` struct. It correctly computes both `potential_voting_power` and `deciding_voting_power` via `potential_and_deciding_voting_power()`, and correctly populates the new dedicated fields. However, the legacy `voting_power` field is assigned `potential_voting_power` instead of `deciding_voting_power`:

```rust
// rs/nns/governance/src/neuron/types.rs, line 932-960
let (potential_voting_power, deciding_voting_power) =
    self.potential_and_deciding_voting_power(voting_power_economics, now_seconds);
...
NeuronInfo {
    ...
    deciding_voting_power: Some(deciding_voting_power),
    potential_voting_power: Some(potential_voting_power),
    voting_power: potential_voting_power,   // <-- BUG: should be deciding_voting_power
    ...
}
```

The Candid interface definition (`rs/nns/governance/canister/governance.did`) documents this field as follows:

```
// Deprecated. Use either deciding_voting_power or potential_voting_power
// instead. Has the same value as deciding_voting_power.
//
// Previously, if a neuron had < 6 months dissolve delay (making it ineligible
// to vote), this would not get set to 0 (zero). That was pretty confusing.
// Now that this is set to deciding_voting_power, this actually does get
// zeroed out.
voting_power : nat64;
```

The comment explicitly states the field "has the same value as `deciding_voting_power`" and that "this is set to `deciding_voting_power`." The code contradicts this: it is set to `potential_voting_power`.

The divergence matters because `deciding_voting_power` applies a time-decay adjustment factor (`deciding_voting_power_adjustment_factor`) that reduces a neuron's effective voting power linearly to zero once the neuron has not refreshed its voting power for between 6 and 7 months (`start_reducing_voting_power_after_seconds` to `start_reducing_voting_power_after_seconds + clear_following_after_seconds`). `potential_voting_power` carries no such reduction. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

Any caller — including dashboards, wallets, dApps, or governance tooling — that reads the `voting_power` field from `NeuronInfo` (returned by the `get_neuron_info` or `list_neurons` query calls) will observe `potential_voting_power`. For a stale neuron (one that has not voted or set following in more than 6 months), `deciding_voting_power` is strictly lower, potentially reaching zero. The caller therefore believes the neuron has more governance influence than it actually exercises. This is a direct analog to the external report: a view/query function returns a value inconsistent with what the protocol actually uses.

The `deciding_voting_power` is what is inserted into ballots at proposal creation time and what is used to tally votes. The `voting_power` field in `NeuronInfo` is what external observers read to assess a neuron's influence. [3](#0-2) [4](#0-3) 

---

### Likelihood Explanation

The entry path requires no privileges: `get_neuron_info` and `list_neurons` are public query endpoints callable by any anonymous or authenticated principal. The Mission 70 voting-power-refresh mechanism is live on mainnet (enabled via `is_mission_70_voting_rewards_enabled()`), meaning stale neurons with reduced `deciding_voting_power` already exist in production. Any tool or user that reads the deprecated `voting_power` field — which the comment explicitly says equals `deciding_voting_power` — will be misled. [5](#0-4) [6](#0-5) 

---

### Recommendation

Change line 960 of `rs/nns/governance/src/neuron/types.rs` from:

```rust
voting_power: potential_voting_power,
```

to:

```rust
voting_power: deciding_voting_power,
```

This aligns the code with the documented intent stated in the Candid interface comment and ensures that any caller reading the deprecated `voting_power` field sees the same value that the protocol actually uses when tallying votes. [7](#0-6) 

---

### Proof of Concept

1. Create a neuron and do not vote or refresh following for more than 6 months (so `voting_power_refreshed_timestamp_seconds` is stale by more than `start_reducing_voting_power_after_seconds = 6 months`).
2. Call `get_neuron_info(<neuron_id>)` as a query (no privileges required).
3. Observe that the returned `NeuronInfo.voting_power` equals `potential_voting_power` (the full, unreduced value).
4. Observe that `NeuronInfo.deciding_voting_power` is strictly less than `voting_power` (reduced by the staleness adjustment factor, potentially to 0).
5. Submit a proposal and observe that the ballot for this neuron is created with `deciding_voting_power`, not `potential_voting_power`.
6. The user who read `voting_power` from the query believed they had more influence than the protocol actually assigned them. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/neuron/types.rs (L383-388)
```rust
        if is_mission_70_voting_rewards_enabled() {
            let eight_year_gang_bonus_base_e8s = self.eight_year_gang_bonus_base_e8s.min(stake_e8s);
            potential_voting_power +=
                Decimal::from(eight_year_gang_bonus_base_e8s) / Decimal::from(10) * boost;
        }

```

**File:** rs/nns/governance/src/neuron/types.rs (L390-399)
```rust
        let adjustment_factor: Decimal = {
            let time_since_last_refreshed = Duration::from_secs(
                now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
            );

            voting_power_economics
                .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
        };

        let deciding_voting_power = adjustment_factor * potential_voting_power.floor();
```

**File:** rs/nns/governance/src/neuron/types.rs (L907-964)
```rust
    /// Get the 'public' information associated with this neuron.
    pub fn get_neuron_info(
        &self,
        voting_power_economics: &VotingPowerEconomics,
        now_seconds: u64,
        requester: PrincipalId,
        multi_query: bool,
    ) -> NeuronInfo {
        let mut recent_ballots = vec![];
        let mut joined_community_fund_timestamp_seconds = None;

        let show_full =
            self.visibility() == Visibility::Public || self.is_hotkey_or_controller(&requester);
        if show_full {
            let mut additional_recent_ballots = self
                .sorted_recent_ballots()
                .into_iter()
                .map(api::BallotInfo::from)
                .collect();
            recent_ballots.append(&mut additional_recent_ballots);

            joined_community_fund_timestamp_seconds = self.joined_community_fund_timestamp_seconds;
        }

        let visibility = Some(self.visibility() as i32);
        let (potential_voting_power, deciding_voting_power) =
            self.potential_and_deciding_voting_power(voting_power_economics, now_seconds);
        let known_neuron_data = if multi_query {
            None
        } else {
            self.known_neuron_data
                .clone()
                .map(api::KnownNeuronData::from)
        };

        NeuronInfo {
            id: Some(self.id()),
            retrieved_at_timestamp_seconds: now_seconds,
            state: self.state(now_seconds) as i32,
            age_seconds: self.age_seconds(now_seconds),
            dissolve_delay_seconds: self.dissolve_delay_seconds(now_seconds),
            recent_ballots,
            created_timestamp_seconds: self.created_timestamp_seconds,
            stake_e8s: self.minted_stake_e8s(),
            joined_community_fund_timestamp_seconds,
            known_neuron_data,
            neuron_type: self.neuron_type,
            visibility,
            voting_power_refreshed_timestamp_seconds: Some(
                self.voting_power_refreshed_timestamp_seconds,
            ),
            deciding_voting_power: Some(deciding_voting_power),
            potential_voting_power: Some(potential_voting_power),
            voting_power: potential_voting_power,
            eight_year_gang_bonus_base_e8s: Some(self.eight_year_gang_bonus_base_e8s),
            staked_maturity_e8s_equivalent: self.staked_maturity_e8s_equivalent,
        }
    }
```

**File:** rs/nns/governance/canister/governance.did (L910-926)
```text
  // Deprecated. Use either deciding_voting_power or potential_voting_power
  // instead. Has the same value as deciding_voting_power.
  //
  // Previously, if a neuron had < 6 months dissolve delay (making it ineligible
  // to vote), this would not get set to 0 (zero). That was pretty confusing.
  // Now that this is set to deciding_voting_power, this actually does get
  // zeroed out.
  voting_power : nat64;

  voting_power_refreshed_timestamp_seconds : opt nat64;
  deciding_voting_power : opt nat64;
  potential_voting_power : opt nat64;
  // See analogous field in Neuron.
  eight_year_gang_bonus_base_e8s : opt nat64;
  // See analogous field in Neuron.
  staked_maturity_e8s_equivalent : opt nat64;
};
```

**File:** rs/nns/governance/canister/governance.did (L1626-1628)
```text
  get_neuron_info : (nat64) -> (Result_5) query;
  get_neuron_info_by_id_or_subaccount : (NeuronIdOrSubaccount) -> (
      Result_5,
```

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L144-159)
```rust
        let mut process_neuron = |neuron: &Neuron| {
            if neuron.is_inactive(now_seconds)
                || neuron.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_seconds
            {
                return;
            }

            let (potential_voting_power, deciding_voting_power) =
                neuron.potential_and_deciding_voting_power(voting_power_economics, now_seconds);
            // We don't handle overflow here, as in `get_voting_power_as_u64` below,
            // the input arguments bigger than u64::MAX will result in an error.
            total_deciding_voting_power =
                total_deciding_voting_power.saturating_add(deciding_voting_power as u128);
            total_potential_voting_power =
                total_potential_voting_power.saturating_add(potential_voting_power as u128);
            voting_power_map.insert(neuron.id().id, deciding_voting_power);
```

**File:** rs/nns/governance/src/network_economics.rs (L296-298)
```rust
    pub const DEFAULT_START_REDUCING_VOTING_POWER_AFTER_SECONDS: u64 = 6 * ONE_MONTH_SECONDS;

    pub const DEFAULT_CLEAR_FOLLOWING_AFTER_SECONDS: u64 = ONE_MONTH_SECONDS;
```
