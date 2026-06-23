### Title
Unpermissioned `ClaimOrRefresh` Neuron Operation Enables Transient DoS on Neuron Ledger Operations - (File: rs/nns/governance/src/governance.rs)

### Summary
The NNS Governance canister allows any unprivileged ingress caller to invoke `ClaimOrRefresh` with `By::NeuronIdOrSubaccount` on any neuron. This operation acquires a per-neuron `in_flight_commands` lock and holds it across an async ledger `account_balance` call. While the lock is held, all other neuron operations — including `Disburse`, `Split`, `DisburseToNeuron`, and `Merge` — are rejected with `LedgerUpdateOngoing`. An attacker who continuously submits `ClaimOrRefresh` messages targeting a victim's neuron can keep the lock occupied, transiently blocking the victim from executing balance-modifying operations.

### Finding Description
`manage_neuron_internal` in `rs/nns/governance/src/governance.rs` handles `ClaimOrRefresh` before any authorization check and returns early: [1](#0-0) 

The `By::NeuronIdOrSubaccount` variant routes to `refresh_neuron_by_id_or_subaccount`, which calls `refresh_neuron`. Inside `refresh_neuron`, a neuron lock is acquired via `lock_neuron_for_command` before the async ledger call: [2](#0-1) 

The lock is stored in `heap_data.in_flight_commands`. Any concurrent attempt to acquire the same lock — including by the neuron's legitimate owner calling `Disburse` or `Split` — is rejected: [3](#0-2) 

The test suite explicitly documents that `ClaimOrRefresh` by subaccount is callable by anyone: [4](#0-3) 

The same pattern exists in SNS Governance, where `manage_neuron_internal` acquires the lock at the top of the function for all commands including `ClaimOrRefresh`, before dispatching to `refresh_neuron`: [5](#0-4) 

SNS `refresh_neuron` also makes an async ledger call while the lock is held: [6](#0-5) 

### Impact Explanation
During the window between lock acquisition and ledger callback completion, the victim neuron owner cannot execute `Disburse`, `Split`, `DisburseToNeuron`, `Merge`, or `Spawn`. An attacker who submits a stream of `ClaimOrRefresh` messages targeting a specific neuron can keep the lock continuously occupied across multiple async round-trips, delaying or preventing time-sensitive neuron operations such as disbursements or splits. The `in_flight_commands` map is persisted in governance state: [7](#0-6) 

If the canister traps during a `ClaimOrRefresh` callback, the lock is intentionally retained permanently (by design, to prevent inconsistent state), which would permanently freeze the neuron: [8](#0-7) 

### Likelihood Explanation
The attack requires no special privileges — any IC principal can submit ingress messages to the NNS or SNS governance canister. The attacker must sustain message throughput sufficient to keep the lock occupied across ledger round-trips. IC ingress rate limits and per-message cycle costs constrain the attack, making indefinite blocking difficult but transient blocking (seconds to minutes) feasible. The SNS governance case is slightly easier to exploit because the lock is acquired at the top of `manage_neuron_internal` before any command-specific logic, extending the lock window.

### Recommendation
- Restrict `ClaimOrRefresh` with `By::NeuronIdOrSubaccount` to callers who hold at least one permission on the target neuron (e.g., `NeuronPermissionType::Vote` or `ManageVotingPermission`), consistent with how SNS already gates other neuron commands via `check_authorized`.
- Alternatively, do not acquire the neuron lock for `ClaimOrRefresh` operations that only read the ledger balance and update `cached_neuron_stake_e8s` upward; the lock is only strictly necessary to prevent concurrent stake-modifying operations.
- Add a rate limit per neuron on `ClaimOrRefresh` invocations to bound the DoS surface even if the operation remains permissionless.

### Proof of Concept
1. Alice owns NNS neuron `N` and wishes to call `Disburse`.
2. Eve submits a stream of `manage_neuron` ingress messages to the NNS governance canister with `Command::ClaimOrRefresh { by: By::NeuronIdOrSubaccount(Empty{}) }` targeting neuron `N`.
3. Each message causes `refresh_neuron` to acquire the `in_flight_commands` lock for `N` and issue an async `account_balance` query to the ICP ledger.
4. While any one of Eve's messages is in-flight, Alice's `Disburse` call hits `lock_neuron_for_command`, finds `N` in `in_flight_commands`, and returns `ErrorType::LedgerUpdateOngoing`.
5. Eve sustains the stream; Alice's disbursement is delayed for the duration of the attack. [3](#0-2) [9](#0-8)

### Citations

**File:** rs/nns/governance/src/governance.rs (L5900-5923)
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
```

**File:** rs/nns/governance/src/governance.rs (L6104-6148)
```rust
        // We run claim or refresh before we check whether a neuron exists because it
        // may not in the case of the neuron being claimed
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
        }
```

**File:** rs/nns/governance/src/neuron_lock.rs (L43-60)
```rust
impl Drop for NeuronAsyncLock {
    fn drop(&mut self) {
        if self.retain {
            return;
        }
        // In the case of a panic, the state of the ledger account representing the neuron's stake
        // may be inconsistent with the internal state of governance.  In that case, we want to
        // prevent further operations with that neuron until the issue can be investigated and
        // resolved, which will require code changes.
        if ic_cdk::futures::is_recovering_from_trap() {
            return;
        }
        // The lock is released when the NeuronAsyncLock is dropped. This is done to ensure that the lock
        // is released even if the NeuronAsyncLock is not explicitly unlocked.
        self.governance.with_borrow_mut(|governance| {
            governance.unlock_neuron(self.neuron_id.id);
        });
    }
```

**File:** rs/nns/governance/src/neuron_lock.rs (L219-238)
```rust
    pub(crate) fn lock_neuron_for_command(
        &mut self,
        id: u64,
        command: NeuronInFlightCommand,
    ) -> Result<LedgerUpdateLock, GovernanceError> {
        if self.heap_data.in_flight_commands.contains_key(&id) {
            return Err(GovernanceError::new_with_message(
                ErrorType::LedgerUpdateOngoing,
                "Neuron has an ongoing ledger update.",
            ));
        }

        self.heap_data.in_flight_commands.insert(id, command);

        Ok(LedgerUpdateLock {
            nid: id,
            gov: self,
            retain: false,
        })
    }
```

**File:** rs/nns/governance/tests/governance.rs (L5014-5029)
```rust
/// Tests that a neuron can be refreshed by subaccount, and that anyone can do
/// it.
#[test]
#[cfg_attr(feature = "tla", with_tla_trace_check)]
fn test_refresh_neuron_by_subaccount_by_controller() {
    let owner = *TEST_NEURON_1_OWNER_PRINCIPAL;
    refresh_neuron_by_id_or_subaccount(owner, owner, RefreshBy::Subaccount);
}

#[test]
#[cfg_attr(feature = "tla", with_tla_trace_check)]
fn test_refresh_neuron_by_subaccount_by_proxy() {
    let owner = *TEST_NEURON_1_OWNER_PRINCIPAL;
    let caller = *TEST_NEURON_1_OWNER_PRINCIPAL;
    refresh_neuron_by_id_or_subaccount(owner, caller, RefreshBy::Subaccount);
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

**File:** rs/sns/governance/src/governance.rs (L4786-4793)
```rust
        // All operations on a neuron exclude each other.
        let _hold = self.lock_neuron_for_command(
            &neuron_id,
            NeuronInFlightCommand {
                timestamp: now,
                command: Some(command.into()),
            },
        )?;
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
