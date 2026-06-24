### Title
Permissionless `ClaimOrRefresh` Allows Any Caller to Dilute Another Neuron's Age Bonus - (File: `rs/nns/governance/src/governance.rs`)

### Summary
The NNS governance `ClaimOrRefresh` command with `By::NeuronIdOrSubaccount` accepts calls from any principal without authorization. An attacker can send ICP to a victim's neuron subaccount and then invoke `ClaimOrRefresh` on behalf of that neuron, triggering `update_stake_adjust_age` and permanently diluting the victim's earned age bonus. This reduces the victim's voting power and proportional share of voting rewards without their consent.

### Finding Description
In `rs/nns/governance/src/governance.rs`, the function `refresh_neuron_by_id_or_subaccount` performs no caller authorization check before proceeding to `refresh_neuron`:

```rust
async fn refresh_neuron_by_id_or_subaccount(
    &mut self,
    id: NeuronIdOrSubaccount,
    claim_or_refresh: &ClaimOrRefresh,
) -> Result<NeuronId, GovernanceError> {
    let (nid, subaccount) = match id { ... };
    self.refresh_neuron(nid, subaccount, claim_or_refresh).await
}
``` [1](#0-0) 

This is reachable from `manage_neuron_internal` via `By::NeuronIdOrSubaccount` without any ownership check: [2](#0-1) 

Inside `refresh_neuron`, when the ledger balance exceeds the cached stake (i.e., someone deposited ICP into the neuron's subaccount), `update_stake_adjust_age` is called:

```rust
Ordering::Less => {
    neuron.update_stake_adjust_age(balance.get_e8s(), now);
}
``` [3](#0-2) 

`update_stake_adjust_age` computes a weighted-average `aging_since_timestamp_seconds` based on the old and new stake. Adding stake dilutes the age proportionally: if a neuron's stake doubles, its effective age is halved. The age bonus in NNS governance can reach up to 25% of base voting power (for neurons aged ≥ 4 years). Diluting it permanently reduces the neuron's `deciding_voting_power` and its share of voting rewards.

The neuron's subaccount is public — it is stored in the neuron's `account` field, queryable by anyone via `list_neurons`. The ICP ledger allows transfers to any account identifier, so the attacker can fund the victim's neuron subaccount without any cooperation from the victim.

The existing test `test_refresh_neuron_by_memo_by_proxy` explicitly confirms that a different caller (`TEST_NEURON_2_OWNER_PRINCIPAL`) can successfully refresh `TEST_NEURON_1_OWNER_PRINCIPAL`'s neuron: [4](#0-3) 

The `ClaimOrRefresh` proto definition confirms `NeuronIdOrSubaccount` is a valid refresh path with no controller field: [5](#0-4) 

### Impact Explanation
A victim neuron that has accumulated years of age bonus (up to 25% voting power multiplier) can have that bonus permanently diluted by an attacker who sends a proportionally large ICP amount to the neuron's subaccount and calls `ClaimOrRefresh`. The victim's absolute stake increases (so they are not financially ruined), but their earned age bonus — which translates directly into a larger share of the NNS voting reward pool — is reduced. Because voting rewards are distributed proportionally to `deciding_voting_power`, diluting the age bonus reduces the victim's reward share in every future reward period for the remaining life of the neuron.

### Likelihood Explanation
The attacker must spend real ICP (which is credited to the victim's neuron, not destroyed). The cost-to-harm ratio is unfavorable for the attacker unless the victim's neuron is small and highly aged. Likelihood is **low**, matching the original report's downgrade to Medium. The attack is permissionless and requires no privileged access, only knowledge of the victim's neuron ID (public) and the ability to transfer ICP.

### Recommendation
Add an authorization check in `refresh_neuron_by_id_or_subaccount` (or in `refresh_neuron`) requiring the caller to be the neuron's controller or a registered hotkey before updating the cached stake and age. Alternatively, decouple the age-adjustment logic from permissionless stake refreshes: allow anyone to trigger a balance sync, but only update `aging_since_timestamp_seconds` when the refresh is initiated by an authorized principal.

### Proof of Concept
1. Alice has a neuron with 100 ICP staked and 4 years of age (≈25% age bonus). Her neuron's subaccount is publicly readable from `list_neurons`.
2. Bob (attacker) transfers 100 ICP to Alice's neuron subaccount via the ICP ledger.
3. Bob calls `manage_neuron` with:
   ```
   ClaimOrRefresh { by: NeuronIdOrSubaccount({}) }
   ```
   targeting Alice's neuron ID. No authorization is required.
4. `refresh_neuron` queries the ledger, finds balance = 200 ICP > cached 100 ICP, and calls `update_stake_adjust_age(200, now)`.
5. Alice's `aging_since_timestamp_seconds` is reset to approximately `now - (100/200) * age`, halving her effective age.
6. Alice's age bonus drops from ≈25% to ≈12.5%. Her `deciding_voting_power` and future reward share are permanently reduced, even though her stake doubled — because the age bonus she earned over 4 years is gone.

### Citations

**File:** rs/nns/governance/src/governance.rs (L5873-5896)
```rust
    /// Refreshes the neuron, getting both it's id and subaccount, if only one
    /// of them was provided as argument.
    async fn refresh_neuron_by_id_or_subaccount(
        &mut self,
        id: NeuronIdOrSubaccount,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let (nid, subaccount) = match id {
            NeuronIdOrSubaccount::NeuronId(neuron_id) => {
                let neuron_subaccount =
                    self.with_neuron(&neuron_id, |neuron| neuron.subaccount())?;
                (neuron_id, neuron_subaccount)
            }
            NeuronIdOrSubaccount::Subaccount(subaccount_bytes) => {
                let subaccount = Self::bytes_to_subaccount(&subaccount_bytes)?;
                let neuron_id = self
                    .neuron_store
                    .get_neuron_id_for_subaccount(subaccount)
                    .ok_or_else(|| Self::no_neuron_for_subaccount_error(&subaccount.0))?;
                (neuron_id, subaccount)
            }
        };
        self.refresh_neuron(nid, subaccount, claim_or_refresh).await
    }
```

**File:** rs/nns/governance/src/governance.rs (L5936-5958)
```rust
        self.with_neuron_mut(&nid, |neuron| {
            match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
                Ordering::Greater => {
                    println!(
                        "{}ERROR. Neuron cached stake was inconsistent.\
                     Neuron account: {} has less e8s: {} than the cached neuron stake: {}.\
                     Stake adjusted.",
                        LOG_PREFIX,
                        account,
                        balance.get_e8s(),
                        neuron.cached_neuron_stake_e8s
                    );
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
                }
                Ordering::Less => {
                    neuron.update_stake_adjust_age(balance.get_e8s(), now);
                }
                // If the stake is the same as the account balance,
                // just return the neuron id (this way this method
                // also serves the purpose of allowing to discover the
                // neuron id based on the memo and the controller).
                Ordering::Equal => (),
            };
```

**File:** rs/nns/governance/src/governance.rs (L6132-6141)
```rust
                Some(By::NeuronIdOrSubaccount(_)) => {
                    let id = mgmt.get_neuron_id_or_subaccount()?.ok_or_else(|| {
                        GovernanceError::new_with_message(
                            ErrorType::NotFound,
                            "No neuron ID specified in the management request.",
                        )
                    })?;
                    self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh)
                        .await
                        .map(ManageNeuronResponse::claim_or_refresh_neuron_response)
```

**File:** rs/nns/governance/tests/governance.rs (L4922-4929)
```rust
/// Tests that a neuron can be refreshed by memo by proxy.
#[test]
#[cfg_attr(feature = "tla", with_tla_trace_check)]
fn test_refresh_neuron_by_memo_by_proxy() {
    let owner = *TEST_NEURON_1_OWNER_PRINCIPAL;
    let caller = *TEST_NEURON_2_OWNER_PRINCIPAL;
    refresh_neuron_by_memo(owner, caller);
}
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L961-965)
```text
      // This just serves as a tag to indicate that the neuron should be
      // refreshed by it's id or subaccount. This does not work to claim
      // new neurons.
      Empty neuron_id_or_subaccount = 3;
    }
```
