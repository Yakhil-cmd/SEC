### Title
Missing Structured Event Emission After Sensitive NNS Governance Parameter Mutation - (File: rs/nns/governance/src/governance.rs)

### Summary
The `perform_manage_network_economics_impl` function in NNS governance silently mutates critical economic parameters (`reject_cost_e8s`, `neuron_minimum_stake_e8s`, `transaction_fee_e8s`, `maximum_node_provider_rewards_e8s`, etc.) with no log statement, no structured event, and no audit-trail entry. Analogously, the SNS governance `set_mode` function transitions the entire governance canister from `PreInitializationSwap` to `Normal` mode without appending any entry to the upgrade journal that already exists for this purpose. Both are direct analogs of the OptimisticGovernor pattern: sensitive, irreversible state changes that produce no observable signal for off-chain clients.

### Finding Description

**NNS Governance — `perform_manage_network_economics_impl`** [1](#0-0) 

The function applies a `ManageNetworkEconomics` proposal and overwrites `self.heap_data.economics` at line 4316 with no `log!`, no `println!`, and no structured event. The outer wrapper `perform_manage_network_economics` (lines 4288–4295) also emits nothing. [2](#0-1) 

The `NetworkEconomics` struct contains fields that govern the entire NNS economy: [3](#0-2) 

**SNS Governance — `set_mode`**

The SNS governance canister exposes `set_mode` as a public update endpoint callable by the swap canister. The canister entry point emits only a bare `log!(INFO, "set_mode")` with no old/new mode values: [4](#0-3) 

The underlying `Governance::set_mode` implementation mutates `self.proto.mode` at line 800 and returns, with no upgrade-journal entry: [5](#0-4) 

The SNS governance canister already maintains an `UpgradeJournal` with a `push_to_upgrade_journal` helper and event types (`TargetVersionSet`, `UpgradeStarted`, `UpgradeOutcome`, etc.) for exactly this purpose: [6](#0-5) [7](#0-6) 

The mode transition (`PreInitializationSwap → Normal`) is not among the tracked event types, so it is permanently invisible to any client querying `get_upgrade_journal`. [8](#0-7) 

### Impact Explanation

**NNS:** Any NNS neuron holder can submit a `ManageNetworkEconomics` proposal. When adopted and executed, `perform_manage_network_economics_impl` silently replaces the live economic parameters. Off-chain dashboards, monitoring bots, and governance-analytics tools that rely on canister logs or structured events to detect parameter changes receive no signal. The old and new values of `reject_cost_e8s`, `neuron_minimum_stake_e8s`, `transaction_fee_e8s`, and `maximum_node_provider_rewards_e8s` are never recorded anywhere in the canister's observable output.

**SNS:** The mode transition is the single most consequential lifecycle event for an SNS — it marks the moment the swap has completed and the DAO becomes fully operational. Because no journal entry is written, the upgrade journal (the canonical audit trail for SNS governance state) contains no record of when or why the mode changed. Any off-chain client that tracks SNS lifecycle via `get_upgrade_journal` will see an incomplete picture.

### Likelihood Explanation

Both code paths are exercised in normal, expected operation:
- Every successfully adopted `ManageNetworkEconomics` NNS proposal triggers `perform_manage_network_economics_impl`.
- Every successfully finalized SNS token swap triggers `set_mode` via the swap canister.

Neither requires any attacker; both are reachable by unprivileged governance participants (neuron holders for NNS, the swap canister for SNS).

### Recommendation

1. **NNS `perform_manage_network_economics_impl`**: Add a `log!(INFO, ...)` call that records the old and new `NetworkEconomics` values before and after the mutation at line 4316.

2. **SNS `set_mode`**: Add a `push_to_upgrade_journal` call inside `Governance::set_mode` (after line 800) recording the old mode, the new mode, and the caller (swap canister principal). Define a new `upgrade_journal_entry::Event` variant (e.g., `ModeChanged`) or reuse a generic event, consistent with the existing journal infrastructure.

### Proof of Concept

**NNS path:**
1. Submit a `ManageNetworkEconomics` proposal changing `reject_cost_e8s` to a new value.
2. Wait for the proposal to be adopted and executed.
3. Query the NNS governance canister logs — no entry records the old or new `reject_cost_e8s` value.
4. The only way to detect the change is to poll `get_network_economics_parameters` before and after execution.

**SNS path:**
1. Complete an SNS token swap (finalize the swap canister).
2. The swap canister calls `set_mode(Normal)` on the SNS governance canister.
3. Query `get_upgrade_journal` on the SNS governance canister — no `ModeChanged` or equivalent entry appears; the journal shows only upgrade-related events.
4. The governance mode transition is permanently unrecorded in the journal. [5](#0-4) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance.rs (L4288-4295)
```rust
    fn perform_manage_network_economics(
        &mut self,
        proposal_id: u64,
        proposed_network_economics: NetworkEconomics,
    ) {
        let result = self.perform_manage_network_economics_impl(proposed_network_economics);
        self.set_proposal_execution_status::<()>(proposal_id, result.map(|()| vec![]));
    }
```

**File:** rs/nns/governance/src/governance.rs (L4297-4318)
```rust
    /// Only call this from perform_manage_network_economics.
    fn perform_manage_network_economics_impl(
        &mut self,
        proposed_network_economics: NetworkEconomics,
    ) -> Result<(), GovernanceError> {
        let new_network_economics = self
            .economics()
            .apply_changes_and_validate(&proposed_network_economics)
            .map_err(|defects| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidProposal,
                    format!(
                        "The resulting NetworkEconomics is invalid for the following reason(s):\
                         \n  - {}",
                        defects.join("\n  - "),
                    ),
                )
            })?;

        self.heap_data.economics = Some(new_network_economics);
        Ok(())
    }
```

**File:** rs/nns/governance/api/src/types.rs (L2107-2150)
```rust
pub struct NetworkEconomics {
    /// The number of E8s (10E-8 of an ICP token) that a rejected
    /// proposal will cost.
    ///
    /// This fee should be controlled by an #Economic proposal type.
    /// The fee does not apply for ManageNeuron proposals.
    pub reject_cost_e8s: u64,
    /// The minimum number of E8s that can be staked in a neuron.
    pub neuron_minimum_stake_e8s: u64,
    /// The number of E8s (10E-8 of an ICP token) that it costs to
    /// employ the 'manage neuron' functionality through proposals. The
    /// cost is incurred by the neuron that makes the 'manage neuron'
    /// proposal and is applied regardless of whether the proposal is
    /// adopted or rejected.
    pub neuron_management_fee_per_proposal_e8s: u64,
    /// The minimum number that the ICP/XDR conversion rate can be set to.
    ///
    /// Measured in XDR (the currency code of IMF SDR) to two decimal
    /// places.
    ///
    /// See /rs/protobuf/def/registry/conversion_rate/v1/conversion_rate.proto
    /// for more information on the rate itself.
    pub minimum_icp_xdr_rate: u64,
    /// The dissolve delay of a neuron spawned from the maturity of an
    /// existing neuron.
    pub neuron_spawn_dissolve_delay_seconds: u64,
    /// The maximum rewards to be distributed to NodeProviders in a single
    /// distribution event, in e8s.
    pub maximum_node_provider_rewards_e8s: u64,
    /// The transaction fee that must be paid for each ledger transaction.
    pub transaction_fee_e8s: u64,
    /// The maximum number of proposals to keep, per topic for eligible topics.
    /// When the total number of proposals for a given topic is greater than this
    /// number, the oldest proposals that have reached a "final" state
    /// may be deleted.
    ///
    /// If unspecified or zero, all proposals are kept.
    pub max_proposals_to_keep_per_topic: u32,
    /// Global Neurons' Fund participation thresholds.
    pub neurons_fund_economics: Option<NeuronsFundEconomics>,

    /// Parameters that affect the voting power of neurons.
    pub voting_power_economics: ::core::option::Option<VotingPowerEconomics>,
}
```

**File:** rs/sns/governance/canister/canister.rs (L537-547)
```rust
/// Sets the mode. Only the swap canister is allowed to call this.
///
/// In practice, the only mode that the swap canister would ever choose is
/// Normal. Also, in practice, the current value of mode should be
/// PreInitializationSwap.  whenever the swap canister calls this.
#[update]
fn set_mode(request: SetMode) -> SetModeResponse {
    log!(INFO, "set_mode");
    governance_mut().set_mode(request.mode, caller());
    SetModeResponse {}
}
```

**File:** rs/sns/governance/src/governance.rs (L785-801)
```rust
    pub fn set_mode(&mut self, mode: i32, caller: PrincipalId) {
        let mode =
            governance::Mode::try_from(mode).unwrap_or_else(|_| panic!("Unknown mode: {mode}"));

        if !self.is_swap_canister(caller) {
            panic!("Caller must be the swap canister.");
        }

        // As of Aug, 2022, the only use-case we have for set_mode is to enter
        // Normal mode (from PreInitializationSwap). Therefore, this is here
        // just to make sure we do not proceed with unexpected operations.
        if mode != governance::Mode::Normal {
            panic!("Entering {mode:?} mode is not allowed.");
        }

        self.proto.mode = mode as i32;
    }
```

**File:** rs/sns/governance/src/upgrade_journal.rs (L117-137)
```rust
impl Governance {
    pub fn push_to_upgrade_journal<Event>(&mut self, event: Event)
    where
        upgrade_journal_entry::Event: From<Event>,
    {
        let event = upgrade_journal_entry::Event::from(event);
        let upgrade_journal_entry = UpgradeJournalEntry {
            event: Some(event),
            timestamp_seconds: Some(self.env.now()),
        };
        match self.proto.upgrade_journal {
            None => {
                self.proto.upgrade_journal = Some(UpgradeJournal {
                    entries: vec![upgrade_journal_entry],
                });
            }
            Some(ref mut journal) => {
                journal.entries.push(upgrade_journal_entry);
            }
        }
    }
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L4115-4128)
```rust
    pub enum Event {
        #[prost(message, tag = "1")]
        UpgradeStepsRefreshed(UpgradeStepsRefreshed),
        #[prost(message, tag = "7")]
        UpgradeStepsReset(UpgradeStepsReset),
        #[prost(message, tag = "2")]
        TargetVersionSet(TargetVersionSet),
        #[prost(message, tag = "3")]
        TargetVersionReset(TargetVersionReset),
        #[prost(message, tag = "4")]
        UpgradeStarted(UpgradeStarted),
        #[prost(message, tag = "5")]
        UpgradeOutcome(UpgradeOutcome),
    }
```

**File:** rs/sns/governance/canister/governance.did (L952-962)
```text
type UpgradeJournalEntry = record {
  event : opt variant {
    UpgradeStepsRefreshed : UpgradeStepsRefreshed;
    UpgradeStepsReset : UpgradeStepsReset;
    TargetVersionSet : TargetVersionSet;
    TargetVersionReset : TargetVersionReset;
    UpgradeStarted : UpgradeStarted;
    UpgradeOutcome : UpgradeOutcome;
  };
  timestamp_seconds : opt nat64;
};
```
