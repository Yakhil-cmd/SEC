### Title
Neuron State (`cached_neuron_stake_e8s`, `neuron_fees_e8s`) Updated After Ledger Inter-Canister Calls in `disburse_neuron` — Read Re-Entrancy Window - (File: `rs/nns/governance/src/governance.rs`, `rs/sns/governance/src/governance.rs`)

---

### Summary

In both the NNS and SNS Governance canisters, the `disburse_neuron` async function performs two sequential ledger inter-canister calls (fee burn, then stake transfer) and updates the neuron's cached state fields (`cached_neuron_stake_e8s`, `neuron_fees_e8s`) **after** each `await` point rather than before. During each await window — which spans at least one consensus round — any query call to the governance canister observes stale, pre-transfer neuron state. This is the IC analog of the Solidity "storage parameters updated after external callback sites" pattern described in the reference report.

---

### Finding Description

**NNS Governance — `disburse_neuron`** (`rs/nns/governance/src/governance.rs`):

The function acquires a neuron lock at line 2031, then:

1. Issues the fee-burn ledger call and **awaits** it (lines 2055–2064). The neuron's `cached_neuron_stake_e8s` and `neuron_fees_e8s` are **not** updated until lines 2067–2075, after the await returns.
2. Issues the stake-disburse ledger call and **awaits** it (lines 2091–2100). The neuron's `cached_neuron_stake_e8s` is **not** updated until lines 2102–2107, after the await returns. [1](#0-0) [2](#0-1) 

**SNS Governance — `disburse_neuron`** (`rs/sns/governance/src/governance.rs`):

The identical ordering exists: fee-burn ledger call awaited at lines 1182–1191, state update at lines 1204–1208; stake-disburse ledger call awaited at lines 1214–1223, state update at lines 1225–1234. [3](#0-2) [4](#0-3) 

The neuron lock (`LedgerUpdateLock` / `NeuronAsyncLock`) prevents concurrent **update** calls from interleaving on the same neuron. [5](#0-4) 

However, the lock has **no effect on query calls**. Query calls are non-replicated and bypass the `in_flight_commands` check entirely. During each await window, any query reading `cached_neuron_stake_e8s` or `neuron_fees_e8s` sees the pre-transfer (stale) value. [6](#0-5) 

The same pattern also appears in `disburse_to_neuron` (NNS), where `cached_neuron_stake_e8s` of the parent neuron is updated at lines 3077–3081 only after the ledger transfer awaited at lines 3040–3049. [7](#0-6) 

---

### Impact Explanation

During each await window (at minimum one consensus round, ~1–2 seconds on mainnet):

- **`cached_neuron_stake_e8s` read re-entrancy**: Any query call to `get_neuron_info`, `list_neurons`, or any other read path that returns neuron stake will report the pre-transfer (inflated) stake. A caller that makes a decision based on this value — e.g., a canister checking whether a neuron meets a minimum stake threshold before taking an action — will act on incorrect data.
- **`neuron_fees_e8s` read re-entrancy**: Between the fee-burn await and the state update, `neuron_fees_e8s` still shows the pre-burn fee amount. Any query reading this field sees stale fees, which could mislead fee-accounting logic in integrating canisters.
- **Inconsistent aggregate views**: Functions that aggregate stake across neurons (e.g., total voting power calculations read via query) will transiently over-count the disbursing neuron's contribution.

The neuron lock correctly prevents a concurrent `disburse`, `split`, or `merge` update call from double-spending the same stake. The residual risk is confined to **read re-entrancy** — stale state observable by unprivileged query callers and integrating canisters during the await window.

---

### Likelihood Explanation

- The NNS Governance canister is one of the most heavily queried canisters on the IC. Governance dashboards, wallets, and integrating canisters continuously poll neuron state via query calls.
- Any query arriving during the ~1–2 second await window (which spans at least one consensus round per ledger call, and `disburse_neuron` makes two sequential ledger calls) will observe stale state.
- The entry path requires only a standard unprivileged `manage_neuron` call with a `Disburse` command from the neuron's controller, which is a normal user action.
- No special privileges, admin keys, or subnet-majority corruption are required.

---

### Recommendation

Apply the checks-effects-interactions pattern: update all cached state fields **before** issuing the ledger inter-canister call, and roll back on error. This mirrors the correct pattern already used in `split_neuron` (NNS), where `cached_neuron_stake_e8s` is decremented before the ledger transfer and refunded on failure: [8](#0-7) 

For `disburse_neuron` in both NNS and SNS governance:

1. **Before** the fee-burn ledger call: set `neuron_fees_e8s = 0` and subtract `fees_amount_e8s` from `cached_neuron_stake_e8s`. On ledger error, restore both fields.
2. **Before** the stake-disburse ledger call: subtract `disburse_amount_e8s + transaction_fee_e8s` from `cached_neuron_stake_e8s`. On ledger error, restore the field.

This ensures that at every await point, the governance state already reflects the intended post-transfer values, eliminating the read re-entrancy window.

---

### Proof of Concept

1. Neuron controller calls `manage_neuron { command: Disburse { ... } }` on NNS Governance.
2. Governance acquires the neuron lock and issues the fee-burn ledger call (first `await`).
3. **During the await** (before the state update at lines 2067–2075), an unprivileged caller issues a query `get_neuron_info` for the same neuron.
4. The query returns `cached_neuron_stake_e8s` at its pre-burn value and `neuron_fees_e8s` still non-zero — both stale.
5. Governance resumes, updates state, then issues the stake-disburse ledger call (second `await`).
6. **During the second await** (before the state update at lines 2102–2107), another query returns `cached_neuron_stake_e8s` still at the post-burn but pre-disburse value — again stale.
7. An integrating canister that caches or acts on either query response during steps 4 or 6 operates on incorrect neuron stake data. [9](#0-8) [1](#0-0) [2](#0-1) [10](#0-9)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2029-2037)
```rust
        // Add the neuron's id to the set of neurons with ongoing ledger updates.
        let now = self.env.now();
        let _neuron_lock = self.lock_neuron_for_command(
            id.id,
            NeuronInFlightCommand {
                timestamp: now,
                command: Some(InFlightCommand::Disburse(disburse.clone())),
            },
        )?;
```

**File:** rs/nns/governance/src/governance.rs (L2055-2076)
```rust
            let _result = self
                .ledger
                .transfer_funds(
                    fees_amount_e8s,
                    0, // Burning transfers don't pay a fee.
                    Some(neuron_subaccount),
                    governance_minting_account(),
                    now,
                )
                .await?;
        }

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

**File:** rs/nns/governance/src/governance.rs (L2269-2311)
```rust
        self.neuron_store.with_neuron_mut(id, |parent_neuron| {
            parent_neuron.cached_neuron_stake_e8s = parent_neuron
                .cached_neuron_stake_e8s
                .checked_sub(split_amount_e8s)
                .expect("Subtracting neuron stake underflows");
        })?;

        let now = self.env.now();
        tla_log_locals! { sn_amount : split_amount_e8s, sn_child_neuron_id: child_nid.id, sn_parent_neuron_id: id.id, sn_child_account_id: tla::account_to_tla(neuron_subaccount(to_subaccount)) };
        let result: Result<u64, NervousSystemError> = self
            .ledger
            .transfer_funds(
                staked_amount,
                transaction_fee_e8s,
                Some(from_subaccount),
                neuron_subaccount(to_subaccount),
                now,
            )
            .await;

        if let Err(error) = result {
            let error = GovernanceError::from(error);

            // Refund the parent neuron if the ledger call somehow failed.
            self.neuron_store
                .with_neuron_mut(id, |parent_neuron| {
                    parent_neuron.cached_neuron_stake_e8s = parent_neuron
                        .cached_neuron_stake_e8s
                        .checked_add(split_amount_e8s)
                        .expect("Neuron stake overflows");
                })
                .expect("Expected the parent neuron to exist");

            // If we've got an error, we assume the transfer didn't happen for
            // some reason. The only state to cleanup is to delete the child
            // neuron, since we haven't mutated the parent yet.
            self.remove_neuron(child_neuron)?;
            println!(
                "Neuron stake transfer of split_neuron: {:?} \
                     failed with error: {:?}. Neuron can't be staked.",
                child_nid, error
            );
            return Err(error);
```

**File:** rs/nns/governance/src/governance.rs (L3040-3086)
```rust
        let result: Result<u64, NervousSystemError> = self
            .ledger
            .transfer_funds(
                staked_amount,
                transaction_fee_e8s,
                Some(from_subaccount),
                neuron_subaccount(to_subaccount),
                memo,
            )
            .await;

        if let Err(error) = result {
            let error = GovernanceError::from(error);
            // If we've got an error, we assume the transfer didn't happen for
            // some reason. The only state to cleanup is to delete the child
            // neuron, since we haven't mutated the parent yet.
            self.remove_neuron(child_neuron)?;
            println!(
                "Neuron minting transfer of to neuron: {:?}\
                                  failed with error: {:?}. Neuron can't be staked.",
                child_nid, error
            );
            return Err(error);
        }

        // Commit the reservation now that the neuron can no longer be deleted.
        if self
            .rate_limiter
            .commit(self.env.now_system_time(), neuron_limit_reservation)
            .is_err()
        {
            println!(
                "{LOG_PREFIX}Warning: Failed to commit rate limiter reservation. This may indicate a bug in the reservation system."
            );
        }

        // Get the neurons again, but this time mutable references.
        self.with_neuron_mut(id, |parent_neuron| {
            // Update the state of the parent and child neurons.
            parent_neuron.cached_neuron_stake_e8s -= disburse_to_neuron.amount_e8s;
        })
        .expect("Neuron not found");

        self.with_neuron_mut(&child_nid, |child_neuron| {
            child_neuron.cached_neuron_stake_e8s = staked_amount;
        })
        .expect("Expected the child neuron to exist");
```

**File:** rs/sns/governance/src/governance.rs (L1182-1235)
```rust
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
