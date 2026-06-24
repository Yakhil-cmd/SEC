### Title
Unprivileged Caller Can Dilute Any NNS Neuron's Age Bonus via Unauthenticated `ClaimOrRefresh` - (File: `rs/nns/governance/src/governance.rs`)

### Summary
The NNS governance `refresh_neuron` function, reachable by any unprivileged ingress sender via `ClaimOrRefresh { by: NeuronIdOrSubaccount }` or `By::MemoAndController`, performs **no authorization check**. An attacker can transfer a small amount of ICP to any victim neuron's public subaccount and then call `manage_neuron` to trigger `update_stake_adjust_age` on that neuron without the owner's consent. This permanently dilutes the victim neuron's `aging_since_timestamp_seconds`, reducing the age bonus (up to 1.25×) applied to voting power and staking rewards.

### Finding Description
In `rs/nns/governance/src/governance.rs`, the `manage_neuron_internal` function handles `ClaimOrRefresh` commands **before** any normal neuron-ownership authorization checks run:

```rust
if let Some(Command::ClaimOrRefresh(claim_or_refresh)) = &mgmt.command {
    return match &claim_or_refresh.by {
        Some(By::NeuronIdOrSubaccount(_)) => {
            let id = mgmt.get_neuron_id_or_subaccount()? ...;
            self.refresh_neuron_by_id_or_subaccount(id, claim_or_refresh).await ...
        }
        Some(By::MemoAndController(memo_and_controller)) =>
            self.claim_or_refresh_neuron_by_memo_and_controller(caller, memo_and_controller.clone(), ...).await ...
    };
}
``` [1](#0-0) 

Neither `refresh_neuron_by_id_or_subaccount` nor the inner `refresh_neuron` function contains any check that the `caller` is the neuron controller or a registered hotkey:

```rust
async fn refresh_neuron(&mut self, nid: NeuronId, subaccount: Subaccount, ...) -> Result<NeuronId, GovernanceError> {
    // No authorization check
    let balance = self.ledger.account_balance(account).await?;
    ...
    self.with_neuron_mut(&nid, |neuron| {
        match neuron.cached_neuron_stake_e8s.cmp(&balance.get_e8s()) {
            Ordering::Less => { neuron.update_stake_adjust_age(balance.get_e8s(), now); }
            ...
        };
    })?;
    Ok(nid)
}
``` [2](#0-1) 

The `update_stake_adjust_age` function reduces the neuron's effective age using a weighted-average formula whenever the stake increases:

```rust
let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
    self.cached_neuron_stake_e8s,
    self.age_seconds(now),
    updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
    0,  // added stake has age 0
);
let new_aging_since_timestamp_seconds = now.saturating_sub(new_age_seconds);
``` [3](#0-2) 

Because every neuron's staking subaccount is a public ICP ledger account (derived deterministically from `controller + memo`), any attacker can transfer ICP to it without the owner's consent. The `By::MemoAndController` path also allows any caller to specify an arbitrary `controller` field, enabling the same attack:

```rust
let controller = memo_and_controller.controller.unwrap_or(*caller);
let memo = memo_and_controller.memo;
let subaccount = ledger::compute_neuron_staking_subaccount(controller, memo);
``` [4](#0-3) 

The same unauthenticated `refresh_neuron` pattern exists in SNS governance: [5](#0-4) 

### Impact Explanation
A neuron's age bonus contributes up to 25% additional voting power and proportionally higher staking rewards. By repeatedly sending small amounts of ICP to a victim's neuron subaccount and calling `ClaimOrRefresh`, an attacker can continuously dilute the victim's `aging_since_timestamp_seconds`. Each refresh permanently resets the age to a weighted average, so a victim neuron that has aged for years can have its age bonus materially reduced. The victim gains the attacker's ICP as additional stake, but the age bonus reduction permanently lowers their future reward rate. For large, long-lived neurons (e.g., known neurons or whale stakers), this constitutes a meaningful governance-weight and reward-rate attack.

**Impact: Medium** (victim gains ICP stake but loses age bonus; net voting power effect is positive for small δ but future reward rate is permanently harmed; attacker must spend real ICP proportional to victim's stake to cause significant damage).

### Likelihood Explanation
The attack requires no special permissions—only the ability to send ICP to a public ledger account and call `manage_neuron`. The victim's neuron subaccount is publicly derivable from their principal and memo. The `ClaimOrRefresh` path bypasses all normal neuron-ownership checks. Any ICP holder can execute this against any neuron at any time.

**Likelihood: Medium** (requires spending ICP proportional to victim's stake for significant impact; small amounts cause minor but real dilution at low cost).

### Recommendation
Add an authorization check inside `refresh_neuron` (or at the `ClaimOrRefresh` dispatch site) requiring that the caller is the neuron controller or a registered hotkey before calling `update_stake_adjust_age`. Alternatively, separate the "update cached stake" operation (which can remain permissionless) from the "adjust age" operation (which should require owner authorization), so that third-party top-ups increase the cached stake without diluting the age.

### Proof of Concept
1. Victim has neuron N with stake S = 100 ICP and age A = 4 years (maximum age bonus 1.25×).
2. Attacker transfers δ = 100 ICP to neuron N's subaccount (a public ICP ledger account).
3. Attacker calls `manage_neuron` with:
   ```
   ClaimOrRefresh { by: NeuronIdOrSubaccount(Empty {}) }
   neuron_id_or_subaccount: NeuronId(N)
   ```
4. `refresh_neuron` reads `balance = 200 ICP > cached_stake = 100 ICP`, calls `update_stake_adjust_age(200 ICP, now)`.
5. `combine_aged_stakes(100, 4_years, 100, 0)` → new age = 2 years.
6. Victim's age bonus drops from 25% to ~12.5%; future reward rate is permanently reduced.
7. Attacker can repeat, each time halving the remaining age, at the cost of ICP that accrues to the victim's neuron. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5857-5860)
```rust
    ) -> Result<NeuronId, GovernanceError> {
        let controller = memo_and_controller.controller.unwrap_or(*caller);
        let memo = memo_and_controller.memo;
        let subaccount = ledger::compute_neuron_staking_subaccount(controller, memo);
```

**File:** rs/nns/governance/src/governance.rs (L5900-5962)
```rust
    async fn refresh_neuron(
        &mut self,
        nid: NeuronId,
        subaccount: Subaccount,
        claim_or_refresh: &ClaimOrRefresh,
    ) -> Result<NeuronId, GovernanceError> {
        let account = neuron_subaccount(subaccount);
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
        let min_stake = self.economics().neuron_minimum_stake_e8s;
        if balance.get_e8s() < min_stake {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Account does not have enough funds to refresh a neuron. \
                     Please make sure that account has at least {:?} e8s (was {:?} e8s)",
                    min_stake,
                    balance.get_e8s()
                ),
            ));
        }
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
        })?;

        Ok(nid)
    }
```

**File:** rs/nns/governance/src/governance.rs (L6106-6147)
```rust
        if let Some(Command::ClaimOrRefresh(claim_or_refresh)) = &mgmt.command {
            // Note that we return here, so none of the rest of this method is executed
            // in this case.
            return match &claim_or_refresh.by {
                Some(By::Memo(memo)) => {
                    let memo_and_controller = MemoAndController {
                        memo: *memo,
                        controller: None,
                    };
                    self.claim_or_refresh_neuron_by_memo_and_controller(
                        caller,
                        memo_and_controller,
                        claim_or_refresh,
                    )
                    .await
                    .map(ManageNeuronResponse::claim_or_refresh_neuron_response)
                }
                Some(By::MemoAndController(memo_and_controller)) => self
                    .claim_or_refresh_neuron_by_memo_and_controller(
                        caller,
                        memo_and_controller.clone(),
                        claim_or_refresh,
                    )
                    .await
                    .map(ManageNeuronResponse::claim_or_refresh_neuron_response),

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
                }
                None => Err(GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    "Need to provide a way by which to claim or refresh the neuron.",
                )),
            };
```

**File:** rs/nns/governance/src/neuron/types.rs (L999-1040)
```rust
    pub fn update_stake_adjust_age(&mut self, updated_stake_e8s: u64, now: u64) {
        // If the updated stake is less than the original stake, preserve the
        // age and distribute it over the new amount. This should not happen
        // in practice, so this code exists merely as a defensive fallback.
        //
        // TODO(NNS1-954) Consider whether update_stake_adjust_age (and other
        // similar methods) should use a neurons effective stake rather than
        // the cached stake.
        if updated_stake_e8s < self.cached_neuron_stake_e8s {
            println!(
                "{}Reducing neuron {:?} stake via update_stake_adjust_age: {} -> {}",
                LOG_PREFIX,
                self.id(),
                self.cached_neuron_stake_e8s,
                updated_stake_e8s
            );
            self.cached_neuron_stake_e8s = updated_stake_e8s;
        } else {
            // If one looks at "stake * age" as describing an area, the goal
            // at this point is to increase the stake while keeping the area
            // constant. This means decreasing the age in proportion to the
            // additional stake, which is the purpose of combine_aged_stakes.
            let (new_stake_e8s, new_age_seconds) = combine_aged_stakes(
                self.cached_neuron_stake_e8s,
                self.age_seconds(now),
                updated_stake_e8s.saturating_sub(self.cached_neuron_stake_e8s),
                0,
            );
            // A consequence of the math above is that the 'new_stake_e8s' is
            // always the same as the 'updated_stake_e8s'. We use
            // 'combine_aged_stakes' here to make sure the age is
            // appropriately pro-rated to accommodate the new stake.
            assert!(new_stake_e8s == updated_stake_e8s);
            self.cached_neuron_stake_e8s = new_stake_e8s;

            let new_aging_since_timestamp_seconds = now.saturating_sub(new_age_seconds);
            let new_disolved_dissolve_state_and_age = self
                .dissolve_state_and_age()
                .adjust_age(new_aging_since_timestamp_seconds);
            self.set_dissolve_state_and_age(new_disolved_dissolve_state_and_age);
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L4210-4227)
```rust
    async fn claim_or_refresh_neuron_by_memo_and_controller(
        &mut self,
        caller: &PrincipalId,
        memo_and_controller: &MemoAndController,
    ) -> Result<(), GovernanceError> {
        let controller = memo_and_controller.controller.unwrap_or(*caller);
        let memo = memo_and_controller.memo;
        let nid = NeuronId::from(ledger::compute_neuron_staking_subaccount_bytes(
            controller, memo,
        ));
        match self.get_neuron_result(&nid) {
            Ok(neuron) => {
                let nid = neuron.id.as_ref().expect("Neuron must have an id").clone();
                self.refresh_neuron(&nid).await
            }
            Err(_) => self.claim_neuron(nid, &controller).await,
        }
    }
```
