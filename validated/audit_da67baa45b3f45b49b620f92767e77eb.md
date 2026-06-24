Audit Report

## Title
SNS Governance `refresh_neuron` Missing Neuron Lock Before Async Ledger Call Enables Cached-Stake Inflation via Concurrent Disburse — (File: `rs/sns/governance/src/governance.rs`)

## Summary

The SNS governance `refresh_neuron` function does not acquire a neuron lock before calling `self.ledger.account_balance(account).await` at line 4256. During the suspension at this await point, the canister can process a concurrent `Disburse` message for the same neuron, which is not blocked because the neuron is absent from `in_flight_commands`. When `refresh_neuron` resumes, it writes the stale pre-disbursal balance into `cached_neuron_stake_e8s`, leaving the neuron with inflated voting power unbacked by actual ICP on the ledger.

## Finding Description

In `rs/sns/governance/src/governance.rs`, `refresh_neuron` performs a pre-await check for `is_neurons_fund_controlled()` and then immediately issues the ledger call without acquiring any lock:

```rust
// L4244-4256
{
    let neuron = self.get_neuron_result(nid)?;
    if neuron.is_neurons_fund_controlled() { ... }
}
// No lock acquired here
let balance = self.ledger.account_balance(account).await?;  // L4256
```

After the await, the stale balance is unconditionally written back:

```rust
// L4273-4288
let neuron = self.get_neuron_result_mut(nid)?;
match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
    Ordering::Greater => { neuron.update_stake(balance.get_e8s(), now); }
    Ordering::Less    => { neuron.update_stake(balance.get_e8s(), now); }
    Ordering::Equal   => (),
};
```

This directly contrasts with two established patterns in the same codebase:

1. **NNS `refresh_neuron`** (`rs/nns/governance/src/governance.rs`, L5907–5919) explicitly calls `self.lock_neuron_for_command(nid.id, ...)` before the ledger await, with the comment: *"We need to lock the neuron to make sure it doesn't undergo concurrent changes while we're checking the balance and refreshing the stake."*

2. **SNS `claim_neuron`** (`rs/sns/governance/src/governance.rs`, L4326–4364) calls `self.add_neuron(neuron.clone())?` before the ledger await, inserting the neuron into `in_flight_commands` to block concurrent mutations, with the comment: *"This avoids a race where a user calls this method a second time before the first time responds."*

`refresh_neuron` does neither. The call chain `manage_neuron` → `claim_or_refresh_neuron_by_memo_and_controller` (L4210–4227) → `refresh_neuron` (L4237) acquires no lock at any level for the existing-neuron path.

The SNS `disburse_neuron` (L1119) does acquire a lock (confirmed by the existing concurrency test at `rs/sns/governance/src/governance/assorted_governance_tests.rs:362–480`, which shows a concurrent `Configure` call receiving `NeuronLocked` while `Disburse` is in-flight). However, because `refresh_neuron` does not lock the neuron, the neuron is absent from `in_flight_commands` when `Disburse` checks it, so `Disburse` proceeds unimpeded during the `refresh_neuron` await window.

## Impact Explanation

An attacker who controls an SNS neuron can simultaneously hold the disbursed ICP in their external account and retain a neuron whose `cached_neuron_stake_e8s` reflects the pre-disbursal balance. Since `cached_neuron_stake_e8s` is the direct input to SNS voting power calculation, the attacker obtains governance influence without corresponding ICP backing. This enables manipulation of SNS governance proposals — including treasury withdrawals, parameter changes, and upgrade proposals — constituting a **High** impact: significant SNS governance security impact with concrete user and protocol harm. If the attacker's inflated voting power is sufficient to reach a proposal-passing threshold (possible in SNS instances with concentrated or low total voting power), the impact escalates toward Critical (theft of SNS treasury assets).

## Likelihood Explanation

No privileged access is required. The attacker needs only: (1) an SNS neuron in Dissolved state (dissolve delay = 0, which is a normal user-reachable state), and (2) the ability to submit two ordinary `manage_neuron` update calls. The IC execution model guarantees that when `refresh_neuron` suspends at the ledger await, the canister will process the next queued message before the callback returns. The attacker can submit both calls in the same ingress batch to ensure the ordering. The attack is deterministic and repeatable.

## Recommendation

Acquire a neuron lock in SNS `refresh_neuron` before the ledger await, mirroring the NNS implementation:

```rust
async fn refresh_neuron(&mut self, nid: &NeuronId) -> Result<(), GovernanceError> {
    // ... neurons_fund_controlled check ...

    // Lock the neuron before the async ledger call
    let _neuron_lock = self.lock_neuron_for_command(nid, NeuronInFlightCommand { ... })?;

    let balance = self.ledger.account_balance(account).await?;
    // ... rest unchanged
}
```

Alternatively, after the await, re-fetch the neuron's current `cached_neuron_stake_e8s` and only apply the update if the balance is strictly greater than the current cached value (never overwrite downward with a potentially stale read). The locking approach is strongly preferred as it is consistent with the established pattern and eliminates the race entirely.

## Proof of Concept

1. Attacker holds SNS neuron N with 100 ICP staked, dissolve delay = 0 (Dissolved state).
2. Attacker submits `manage_neuron { ClaimOrRefresh { by: NeuronIdOrSubaccount } }` → dispatches to `refresh_neuron` → canister suspends at `ledger.account_balance(account).await` (L4256). Neuron is **not** in `in_flight_commands`.
3. During suspension, attacker submits `manage_neuron { Disburse { amount: 100 ICP, to: attacker_account } }` → `manage_neuron` checks `in_flight_commands` → neuron absent → `disburse_neuron` proceeds → two ledger transfers execute → neuron subaccount balance ≈ 0 → `cached_neuron_stake_e8s` updated to 0 by `Disburse`.
4. Ledger responds to the `account_balance` query with the pre-disbursal value of 100e8 → `refresh_neuron` resumes → `balance.get_e8s()` = 100e8, `neuron.cached_neuron_stake_e8s` = 0 → hits `Ordering::Less` branch → calls `neuron.update_stake(100e8, now)`.
5. Final state: attacker holds 100 ICP in external account AND neuron N has `cached_neuron_stake_e8s = 100e8` with actual ledger balance ≈ 0.
6. Attacker uses inflated voting power to submit and pass SNS governance proposals.

A deterministic integration test using PocketIC or the existing `TestLedger` pattern (as in `assorted_governance_tests.rs`) can reproduce this by: (a) starting `refresh_neuron` and pausing the mock ledger before responding to `account_balance`, (b) running `disburse_neuron` to completion, (c) releasing the mock ledger response, and (d) asserting that `cached_neuron_stake_e8s > 0` while the actual subaccount balance = 0. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1119-1137)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
        // First check authorized
        let neuron = self.get_neuron_result(id)?;
        neuron.check_authorized(caller, NeuronPermissionType::Disburse)?;

        // Check that the neuron is dissolved.
        let state = neuron.state(self.env.now());
        if state != NeuronState::Dissolved {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {id} is NOT dissolved. It is in state {state:?}"),
            ));
        }

```

**File:** rs/sns/governance/src/governance.rs (L4237-4256)
```rust
    async fn refresh_neuron(&mut self, nid: &NeuronId) -> Result<(), GovernanceError> {
        let now = self.env.now();
        let subaccount = nid.subaccount()?;
        let account = self.neuron_account_id(subaccount);

        // First ensure that the neuron was not created via an NNS Neurons' Fund participation in the
        // decentralization swap
        {
            let neuron = self.get_neuron_result(nid)?;

            if neuron.is_neurons_fund_controlled() {
                return Err(GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    "Cannot refresh an SNS Neuron controlled by the Neurons' Fund",
                ));
            }
        }

        // Get the balance of the neuron from the ledger canister.
        let balance = self.ledger.account_balance(account).await?;
```

**File:** rs/sns/governance/src/governance.rs (L4273-4295)
```rust
        let neuron = self.get_neuron_result_mut(nid)?;
        match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
            Ordering::Greater => {
                log!(
                    ERROR,
                    "ERROR. Neuron cached stake was inconsistent.\
                     Neuron account: {} has less e8s: {} than the cached neuron stake: {}.\
                     Stake adjusted.",
                    account,
                    balance.get_e8s(),
                    neuron.cached_neuron_stake_e8s
                );
                neuron.update_stake(balance.get_e8s(), now);
            }
            Ordering::Less => {
                neuron.update_stake(balance.get_e8s(), now);
            }
            // If the stake is the same as the account balance,
            // just return the neuron id (this way this method
            // also serves the purpose of allowing to discover the
            // neuron id based on the memo and the controller).
            Ordering::Equal => (),
        };
```

**File:** rs/sns/governance/src/governance.rs (L4326-4364)
```rust
        // We need to create the neuron before checking the balance so that we record
        // the neuron and add it to the set of neurons with ongoing operations. This
        // avoids a race where a user calls this method a second time before the first
        // time responds. If we store the neuron and lock it before we make the call,
        // we know that any concurrent call to mutate the same neuron will need to wait
        // for this one to finish before proceeding.
        let neuron = Neuron {
            id: Some(neuron_id.clone()),
            permissions: vec![NeuronPermission::new(
                principal_id,
                self.neuron_claimer_permissions_or_panic().permissions,
            )],
            cached_neuron_stake_e8s: 0,
            neuron_fees_e8s: 0,
            created_timestamp_seconds: now,
            aging_since_timestamp_seconds: now,
            followees: self.default_followees_or_panic().followees,
            topic_followees: Some(TopicFollowees {
                topic_id_to_followees: btreemap! {},
            }),
            maturity_e8s_equivalent: 0,
            dissolve_state: Some(DissolveState::DissolveDelaySeconds(0)),
            // A neuron created through the `claim_or_refresh` ManageNeuron command will
            // have the default voting power multiplier applied.
            voting_power_percentage_multiplier: DEFAULT_VOTING_POWER_PERCENTAGE_MULTIPLIER,
            source_nns_neuron_id: None,
            staked_maturity_e8s_equivalent: None,
            auto_stake_maturity: None,
            vesting_period_seconds: None,
            disburse_maturity_in_progress: vec![],
        };

        // This also verifies that there are not too many neurons already.
        self.add_neuron(neuron.clone())?;

        // Get the balance of the neuron's subaccount from ledger canister.
        let subaccount = neuron_id.subaccount()?;
        let account = self.neuron_account_id(subaccount);
        let balance = self.ledger.account_balance(account).await?;
```

**File:** rs/nns/governance/src/governance.rs (L5907-5923)
```rust
        // We need to lock the neuron to make sure it doesn't undergo
        // concurrent changes while we're checking the balance and
        // refreshing the stake.
        let now = self.env.now();
        let _neuron_lock = self.lock_neuron_for_command(
            nid.id,
            NeuronInFlightCommand {
                timestamp: now,
                command: Some(InFlightCommand::ClaimOrRefreshNeuron(
                    claim_or_refresh.clone(),
                )),
            },
        )?;

        // Get the balance of the neuron from the ledger canister.
        tla_log_locals! { neuron_id: nid.id };
        let balance = self.ledger.account_balance(account).await?;
```

**File:** rs/sns/governance/src/governance/assorted_governance_tests.rs (L434-473)
```rust
            transfer_funds_arrived.notified().await;
            // It is now guaranteed that disburse is now in mid flight.

            // Step 2.2: Begin another manage_neuron call.
            let configure = ManageNeuron {
                subaccount: user.subaccount.to_vec(),
                command: Some(manage_neuron::Command::Configure(
                    manage_neuron::Configure {
                        operation: Some(
                            manage_neuron::configure::Operation::IncreaseDissolveDelay(
                                manage_neuron::IncreaseDissolveDelay {
                                    additional_dissolve_delay_seconds: 42,
                                },
                            ),
                        ),
                    },
                )),
            };
            let configure_result = unsafe {
                raw_governance
                    .as_mut()
                    .unwrap()
                    .manage_neuron(&configure, &principal_id)
                    .await
            };

            // Step 3: Inspect results.

            // Assert that configure_result is NeuronLocked.
            match &configure_result.command.as_ref().unwrap() {
                manage_neuron_response::Command::Error(err) => {
                    assert_eq!(
                        err.error_type,
                        ErrorType::NeuronLocked as i32,
                        "err: {:#?}",
                        err,
                    );
                }
                _ => panic!("configure_result: {configure_result:#?}"),
            }
```
