### Title
Stale `cached_neuron_stake_e8s` During Second Ledger Await in `disburse_neuron` Exposes Inflated Stake to Query Callers - (`rs/nns/governance/src/governance.rs`)

---

### Summary

`disburse_neuron` in NNS Governance (and identically in SNS Governance) makes two sequential inter-canister ledger calls. Between the second call being dispatched and its response being processed, the governance canister's `cached_neuron_stake_e8s` still reflects the pre-disburse value even though the ICP has already been transferred out of the neuron's ledger account. Any query call to `get_neuron_info` during this window returns an inflated `stake_e8s`, creating a state-inconsistency window exploitable by interacting protocols.

---

### Finding Description

`disburse_neuron` in NNS Governance performs two sequential ledger transfers with state updates interleaved:

1. **Transfer 1** – burn neuron management fees (`.await?`)
2. **Synchronous state update** – reduce `cached_neuron_stake_e8s` by `fees_amount_e8s`, zero `neuron_fees_e8s`
3. **Transfer 2** – disburse stake to recipient (`.await?`) ← funds leave the ledger account here
4. **Synchronous state update** – reduce `cached_neuron_stake_e8s` by `disburse_amount_e8s + transaction_fee_e8s` [1](#0-0) 

The critical window is **step 3**: the second `transfer_funds` call is dispatched to the ledger canister. At this point the ICP has already left the neuron's subaccount on the ledger, but the governance canister's state still shows the pre-disburse `cached_neuron_stake_e8s`. Because IC canisters commit state at each `await` boundary, this intermediate state is observable by concurrent messages — including query calls — during the inter-canister round trip. [2](#0-1) 

The public query `get_neuron_info` returns `stake_e8s` computed as `minted_stake_e8s()` = `cached_neuron_stake_e8s - neuron_fees_e8s`. During the window, this returns the post-fee-burn but pre-disburse value — an inflated figure that does not match the actual ledger balance. [3](#0-2) [4](#0-3) 

The neuron lock (`in_flight_commands`) prevents concurrent `manage_neuron` update calls from double-disbursing, but it does **not** prevent query calls from reading the stale state. Crucially, `get_neuron_info` (the publicly accessible query) does not expose `in_flight_commands`, so an external protocol has no standard way to detect the inconsistency window. [5](#0-4) 

The identical pattern exists in SNS Governance: [6](#0-5) 

The NNS Governance proto explicitly documents the `in_flight_commands` lock as the mechanism preventing interleaving, but this only covers update-path callers: [7](#0-6) 

---

### Impact Explanation

**Concrete state inconsistency during Transfer 2 in-flight (example with 100 ICP stake, 10 ICP fees):**

| Phase | `cached_neuron_stake_e8s` | `neuron_fees_e8s` | `get_neuron_info.stake_e8s` | Actual ledger balance |
|---|---|---|---|---|
| Before disburse | 100 | 10 | 90 | 100 |
| After Transfer 1 + state update | 90 | 0 | 90 | 90 |
| **Transfer 2 in-flight** | **90** | **0** | **90 (INFLATED)** | **~0** |
| After Transfer 2 + state update | 0 | 0 | 0 | 0 |

Any DeFi protocol on the IC that:
- Accepts neurons as collateral based on `get_neuron_info.stake_e8s`
- Allows borrowing against that collateral
- Reads neuron stake via (non-certified) query

…can be exploited by a neuron controller who times a `disburse_neuron` call and simultaneously interacts with the protocol during the Transfer 2 window. The attacker receives the disbursed ICP, the protocol sees 90 ICP collateral, and after the window closes the collateral is worthless.

This is the direct IC analog of the Sablier report: funds have been sent to the recipient (ledger transfer dispatched), but the accounting in the governance canister has not yet been updated, creating a window where interacting protocols observe incorrect state.

---

### Likelihood Explanation

**Medium-low.** The window is bounded by one inter-canister call round trip (~2 seconds on mainnet). The attacker must control a dissolved neuron and have access to an interacting protocol that reads `get_neuron_info` via query without checking `in_flight_commands`. As the IC DeFi ecosystem grows and more protocols accept neurons as collateral (e.g., lending protocols, vaults), the exploitability increases. The attacker-controlled entry path is a standard unprivileged ingress call to `manage_neuron` with `Disburse` command.

---

### Recommendation

Apply the checks-effects-interactions pattern: update `cached_neuron_stake_e8s` **before** dispatching Transfer 2, not after. Specifically, deduct `disburse_amount_e8s + transaction_fee_e8s` from `cached_neuron_stake_e8s` synchronously before the second `transfer_funds(...).await`, and restore it on failure (as already done for `split_neuron` in `NeuronStakeTransferOperation::transfer_neuron_stake_with_ledger`): [8](#0-7) 

This pattern — deduct before the call, refund on error — eliminates the inconsistency window. Apply the same fix to SNS Governance `disburse_neuron`.

---

### Proof of Concept

**Entry path:** Unprivileged ingress sender controlling a dissolved NNS neuron.

1. Attacker deploys a canister `LendingProtocol` that calls `get_neuron_info` (query) on NNS Governance and offers loans against `stake_e8s`.
2. Attacker stakes 100 ICP into a neuron, accumulates 10 ICP in `neuron_fees_e8s`.
3. Attacker calls `manage_neuron { command: Disburse { ... } }` on NNS Governance.
4. NNS Governance executes Transfer 1 (burn 10 ICP fees), updates state: `cached_neuron_stake_e8s=90`, `neuron_fees_e8s=0`.
5. NNS Governance dispatches Transfer 2 (send ~90 ICP to attacker's account) — **funds leave the ledger account**.
6. While Transfer 2 is in-flight, attacker calls `LendingProtocol.borrow()`. The protocol calls `get_neuron_info` (query), receives `stake_e8s=90`, and grants a loan of up to 90 ICP.
7. Transfer 2 completes; `cached_neuron_stake_e8s` drops to 0.
8. Attacker holds ~90 ICP from disburse + loan proceeds; `LendingProtocol` holds a neuron with 0 stake as collateral.

The root cause — `cached_neuron_stake_e8s` not updated before Transfer 2 is dispatched — is confirmed at: [1](#0-0)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2067-2076)
```rust
        self.with_neuron_mut(id, |neuron| {
            // Update the stake and the fees to reflect the burning above.
            if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
                neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
            } else {
                neuron.cached_neuron_stake_e8s = 0;
            }
            neuron.neuron_fees_e8s = 0;
        })
        .expect("Expected the parent neuron to exist");
```

**File:** rs/nns/governance/src/governance.rs (L2091-2108)
```rust
        let block_height = self
            .ledger
            .transfer_funds(
                disburse_amount_e8s,
                transaction_fee_e8s,
                Some(neuron_subaccount),
                to_account,
                now,
            )
            .await?;

        self.with_neuron_mut(id, |neuron| {
            let to_deduct = disburse_amount_e8s + transaction_fee_e8s;
            // The transfer was successful we can change the stake of the neuron.
            neuron.cached_neuron_stake_e8s =
                neuron.cached_neuron_stake_e8s.saturating_sub(to_deduct);
        })
        .expect("Expected the parent neuron to exist");
```

**File:** rs/nns/governance/src/neuron/types.rs (L942-964)
```rust
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

**File:** rs/nns/governance/src/neuron/types.rs (L981-986)
```rust
    /// Returns the current `minted` stake of the neuron, i.e. the ICP backing the
    /// neuron, minus the fees. This does not count staked maturity.
    pub fn minted_stake_e8s(&self) -> u64 {
        self.cached_neuron_stake_e8s
            .saturating_sub(self.neuron_fees_e8s)
    }
```

**File:** rs/nns/governance/canister/governance.did (L889-926)
```text
type NeuronInfo = record {
  id: opt NeuronId;
  dissolve_delay_seconds : nat64;
  recent_ballots : vec BallotInfo;
  neuron_type : opt int32;
  created_timestamp_seconds : nat64;
  state : int32;

  // The amount of ICP (and staked maturity) locked in this neuron.
  //
  // This is the foundation of the neuron's voting power.
  //
  // cached_neuron_stake_e8s - neuron_fees_e8s + staked_maturity_e8s_equivalent
  stake_e8s : nat64;

  joined_community_fund_timestamp_seconds : opt nat64;
  retrieved_at_timestamp_seconds : nat64;
  visibility : opt int32;
  known_neuron_data : opt KnownNeuronData;
  age_seconds : nat64;

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

**File:** rs/sns/governance/src/governance.rs (L1181-1234)
```rust
        if max_burnable_fee > transaction_fee_e8s {
            let _result = self
                .ledger
                .transfer_funds(
                    max_burnable_fee,
                    0, // Burning transfers don't pay a fee.
                    Some(from_subaccount),
                    self.governance_minting_account(),
                    self.env.now(),
                )
                .await?;

            // We only update the cached_neuron_stake_e8s and neuron_fees_e8s if we actually
            // burn fees, otherwise this leads to ledger and governance getting out of sync.
            let nid = id.to_string();
            let neuron = self
                .proto
                .neurons
                .get_mut(&nid)
                .expect("Expected the parent neuron to exist");

            // Update the neuron's stake and management fees to reflect the burning
            // above.
            neuron.cached_neuron_stake_e8s = neuron
                .cached_neuron_stake_e8s
                .saturating_sub(max_burnable_fee);

            neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
        }

        // Transfer 2 - Disburse to the chosen account. This may fail if the
        // user told us to disburse more than they had in their account (but
        // the burn still happened).
        let block_height = self
            .ledger
            .transfer_funds(
                disburse_amount_e8s,
                transaction_fee_e8s,
                Some(from_subaccount),
                to_account,
                self.env.now(),
            )
            .await?;

        let nid = id.to_string();
        let neuron = self
            .proto
            .neurons
            .get_mut(&nid)
            .expect("Expected the parent neuron to exist");

        let to_deduct = disburse_amount_e8s + transaction_fee_e8s;
        // The transfer was successful we can change the stake of the neuron.
        neuron.cached_neuron_stake_e8s = neuron.cached_neuron_stake_e8s.saturating_sub(to_deduct);
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L2155-2175)
```text
  // Set of in-flight neuron ledger commands.
  //
  // Whenever we issue a ledger transfer (for disburse, split, spawn etc)
  // we store it in this map, keyed by the id of the neuron being changed
  // and remove the entry when it completes.
  //
  // An entry being present in this map acts like a "lock" on the neuron
  // and thus prevents concurrent changes that might happen due to the
  // interleaving of user requests and callback execution.
  //
  // If there are no ongoing requests, this map should be empty.
  //
  // If something goes fundamentally wrong (say we trap at some point
  // after issuing a transfer call) the neuron(s) involved are left in a
  // "locked" state, meaning new operations can't be applied without
  // reconciling the state.
  //
  // Because we know exactly what was going on, we should have the
  // information necessary to reconcile the state, using custom code
  // added on upgrade, if necessary.
  map<fixed64, NeuronInFlightCommand> in_flight_commands = 10;
```

**File:** rs/nns/governance/src/governance/ledger_helper.rs (L97-131)
```rust
        // This is the first mutation step and therefore recoverable if it fails.
        neuron_store.with_neuron_mut(&self.source_neuron_id, |source_neuron| {
            self.subtract_stake_from_source(source_neuron);
        })?;

        // If the ledger call fails, we try to refund the stake to the source neuron, and it would
        // be recoverable if the refund succeeds.
        ledger
            .transfer_funds(
                self.amount_to_target_e8s,
                self.transaction_fees_e8s,
                Some(source_subaccount),
                neuron_subaccount(target_subaccount),
                now_seconds,
            )
            .await
            .map_err(|err| {
                // Refund the stake to the source neuron.
                neuron_store
                    .with_neuron_mut(&self.source_neuron_id, |source_neuron| {
                        self.add_stake_to_source(source_neuron);
                    })
                    .expect("Source neuron not found after failing to transfer stake");
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Failed to transfer stake: {err}"),
                )
            })?;

        neuron_store
            .with_neuron_mut(&self.target_neuron_id, |target_neuron| {
                self.add_stake_to_target(target_neuron);
            })
            .expect("Target neuron not found after transferring stake");
        Ok(())
```
