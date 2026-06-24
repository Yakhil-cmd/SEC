### Title
`NeuronInfo.voting_power` Displays `potential_voting_power` Instead of `deciding_voting_power`, Contradicting DID Documentation - (File: `rs/nns/governance/src/neuron/types.rs`)

---

### Summary

The `NeuronInfo.voting_power` field returned by `get_neuron_info` is assigned `potential_voting_power` in the implementation, while the NNS governance DID interface explicitly documents it as having "the same value as `deciding_voting_power`." For neurons that have not refreshed their voting power in more than 6 months, `potential_voting_power` can be significantly higher than `deciding_voting_power` — which linearly decays to zero. This creates a display/API inconsistency directly analogous to the WLFI `getVotes`/`getPastVotes` discrepancy: the value surfaced to callers overstates actual governance influence.

---

### Finding Description

In `Neuron::get_neuron_info()`, the `NeuronInfo` struct is populated with:

```rust
voting_power: potential_voting_power,
``` [1](#0-0) 

However, the canonical DID interface documents this field as:

```
// Deprecated. Use either deciding_voting_power or potential_voting_power
// instead. Has the same value as deciding_voting_power.
// ...
// Now that this is set to deciding_voting_power, this actually does get zeroed out.
voting_power : nat64;
``` [2](#0-1) 

The DID states the field "Has the same value as `deciding_voting_power`" and that "this is set to `deciding_voting_power`." The code contradicts this by assigning `potential_voting_power`.

The two values diverge for any neuron whose `voting_power_refreshed_timestamp_seconds` is more than `start_reducing_voting_power_after_seconds` (currently 6 months) in the past. In that regime, `deciding_voting_power_adjustment_factor` decreases linearly from 1.0 to 0.0 over the subsequent `clear_following_after_seconds` (currently 1 month):

```rust
let adjustment_factor: Decimal = {
    let time_since_last_refreshed = Duration::from_secs(
        now_seconds.saturating_sub(self.voting_power_refreshed_timestamp_seconds),
    );
    voting_power_economics
        .deciding_voting_power_adjustment_factor(time_since_last_refreshed)
};
let deciding_voting_power = adjustment_factor * potential_voting_power.floor();
``` [3](#0-2) 

The actual proposal ballots are correctly computed using `deciding_voting_power`:

```rust
voting_power_map.insert(neuron.id().id, deciding_voting_power);
``` [4](#0-3) 

So the governance canister's internal accounting is correct, but the value exposed via the public `get_neuron_info` query — and consumed by every dashboard, wallet, and third-party integration — is `potential_voting_power`, not `deciding_voting_power`.

The `get_neuron_info` endpoint is publicly accessible without authorization:

```rust
pub fn get_neuron_info(
    &self,
    id: &NeuronId,
    requester: PrincipalId,
) -> Result<NeuronInfo, GovernanceError> {
    let now = self.env.now();
    self.with_neuron(id, |neuron| {
        neuron.get_neuron_info(self.voting_power_economics(), now, requester, false)
    })
}
``` [5](#0-4) 

The Rosetta API also surfaces this field directly to external consumers:

```rust
voting_power: res.voting_power,
``` [6](#0-5) 

---

### Impact Explanation

Any neuron that has not performed a refresh action (direct vote, set following, or explicit `RefreshVotingPower`) in more than 6 months will have `deciding_voting_power` < `potential_voting_power`, potentially reaching 0. The `voting_power` field returned by `get_neuron_info` will show the full `potential_voting_power`, while the neuron's actual ballot weight in any proposal is 0. Third-party integrations, dashboards, and the Rosetta API that consume `voting_power` per the DID contract will display and act on an inflated, incorrect value. Neuron owners may believe they retain full governance influence and choose not to refresh, when in fact their deciding power has already decayed to zero.

The actual on-chain governance outcomes are not corrupted — ballots are correctly assigned `deciding_voting_power`. The impact is confined to the public query surface and any off-chain logic that trusts the `voting_power` field.

---

### Likelihood Explanation

The NNS has a large population of neurons that vote exclusively via following and never interact directly. Any such neuron that has not set following or voted directly in more than 6 months will exhibit this discrepancy. The `VotingPowerEconomics` defaults (`start_reducing_voting_power_after_seconds` = 6 months, `clear_following_after_seconds` = 1 month) are active on mainnet. The discrepancy is therefore reachable by any unprivileged query caller reading neuron state for a stale neuron.

---

### Recommendation

Change line 960 of `rs/nns/governance/src/neuron/types.rs` to assign `deciding_voting_power` instead of `potential_voting_power`, matching the DID documentation:

```rust
voting_power: deciding_voting_power,
```

Alternatively, if the intent is to keep `voting_power` as `potential_voting_power`, update the DID documentation to accurately reflect this, and ensure all downstream consumers (Rosetta, dashboards) are aware of the distinction and use `deciding_voting_power` for governance-relevant display.

---

### Proof of Concept

1. Stake a neuron with sufficient dissolve delay (≥ 6 months).
2. Do not perform any refresh action (no direct vote, no set-following, no `RefreshVotingPower`) for more than 7 months.
3. Call `get_neuron_info` (publicly accessible, no authorization required).
4. Observe: `voting_power` == `potential_voting_power` > 0, while `deciding_voting_power` == 0.
5. The DID contract states `voting_power` "Has the same value as `deciding_voting_power`" — the code violates this contract.

The test at `rs/nns/governance/src/neuron/types/tests.rs` line 211 confirms the current (incorrect) behavior:

```rust
voting_power: potential_voting_power,
``` [7](#0-6)

### Citations

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

**File:** rs/nns/governance/src/neuron/types.rs (L958-960)
```rust
            deciding_voting_power: Some(deciding_voting_power),
            potential_voting_power: Some(potential_voting_power),
            voting_power: potential_voting_power,
```

**File:** rs/nns/governance/canister/governance.did (L910-917)
```text
  // Deprecated. Use either deciding_voting_power or potential_voting_power
  // instead. Has the same value as deciding_voting_power.
  //
  // Previously, if a neuron had < 6 months dissolve delay (making it ineligible
  // to vote), this would not get set to 0 (zero). That was pretty confusing.
  // Now that this is set to deciding_voting_power, this actually does get
  // zeroed out.
  voting_power : nat64;
```

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L151-159)
```rust
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

**File:** rs/nns/governance/src/governance.rs (L3264-3273)
```rust
    pub fn get_neuron_info(
        &self,
        id: &NeuronId,
        requester: PrincipalId,
    ) -> Result<NeuronInfo, GovernanceError> {
        let now = self.env.now();
        self.with_neuron(id, |neuron| {
            neuron.get_neuron_info(self.voting_power_economics(), now, requester, false)
        })
    }
```

**File:** rs/rosetta-api/icp/src/request_handler.rs (L922-931)
```rust
        Ok(NeuronInfoResponse {
            verified_query: verified,
            retrieved_at_timestamp_seconds: res.retrieved_at_timestamp_seconds,
            state,
            age_seconds: res.age_seconds,
            dissolve_delay_seconds: res.dissolve_delay_seconds,
            voting_power: res.voting_power,
            created_timestamp_seconds: res.created_timestamp_seconds,
            stake_e8s: res.stake_e8s,
        })
```

**File:** rs/nns/governance/src/neuron/types/tests.rs (L211-211)
```rust
            voting_power: potential_voting_power,
```
