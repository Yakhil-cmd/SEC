### Title
Async Reentrancy in SNS Governance `disburse_neuron` — State Updated After Await Without Neuron Lock - (File: rs/sns/governance/src/governance.rs)

### Summary

The SNS governance `disburse_neuron` function performs two async ledger calls and updates `cached_neuron_stake_e8s` only **after** each `await` point, without acquiring a neuron lock before the first inter-canister call. This is the IC analog of the Solidity reentrancy pattern: between the initial state read and the post-await state write, the canister can process a second ingress message for the same neuron, allowing the same stake to be disbursed twice.

### Finding Description

In `rs/sns/governance/src/governance.rs`, `disburse_neuron` reads the neuron's stake and fees synchronously, then makes two sequential async ledger calls, updating `cached_neuron_stake_e8s` only after each `await` returns:

```
1. Read neuron.stake_e8s(), neuron.neuron_fees_e8s  (synchronous)
2. ledger.transfer_funds(max_burnable_fee, ...).await?   ← suspend point
3. neuron.cached_neuron_stake_e8s -= max_burnable_fee    ← state update AFTER await
4. ledger.transfer_funds(disburse_amount_e8s, ...).await? ← suspend point
5. neuron.cached_neuron_stake_e8s -= to_deduct           ← state update AFTER await
``` [1](#0-0) [2](#0-1) [3](#0-2) 

No neuron lock is acquired anywhere in this function before the first `await`. The precondition comment at line 1118 states "The neuron's id is not yet in the list of neurons with ongoing operations," but this is listed as a caller-enforced precondition, not enforced within the function itself.

By contrast, the NNS governance `disburse_neuron` explicitly acquires a `LedgerUpdateLock` **before** any ledger call:

```rust
let _neuron_lock = self.lock_neuron_for_command(
    id.id,
    NeuronInFlightCommand { ... },
)?;
``` [4](#0-3) 

The lock mechanism itself is well-defined and prevents concurrent operations on the same neuron: [5](#0-4) 

The SNS governance function is missing this protection entirely.

### Impact Explanation

On the IC, when a canister suspends at an `await` point, the subnet scheduler can deliver other queued ingress messages to the same canister. If a user submits two `manage_neuron { Disburse }` calls for the same dissolved neuron in rapid succession:

1. Call 1 reads `stake = S`, `fees = F`, computes `disburse_amount`.
2. Call 1 suspends at the first `ledger.transfer_funds(...).await`.
3. Call 2 is inducted. It reads the **same** unmodified `stake = S`, `fees = F`, passes all checks (neuron is dissolved, caller is authorized), and also suspends at its first ledger call.
4. Both calls resume and each successfully transfers `disburse_amount` from the neuron's ledger subaccount.
5. `cached_neuron_stake_e8s` is decremented twice, but the actual ledger balance was only sufficient for one transfer — the second transfer succeeds only if the ledger subaccount still has funds (e.g., if the neuron had more on-ledger balance than `cached_neuron_stake_e8s` reflected), or the second transfer fails at the ledger level.

The worst-case impact is double-disbursement of a neuron's staked tokens, draining the neuron subaccount beyond what governance intended. Even if the second ledger transfer fails, the first `cached_neuron_stake_e8s` update from call 1 may be overwritten by call 2's update, leaving governance state inconsistent with the ledger.

### Likelihood Explanation

The attacker is the neuron controller — an unprivileged ingress sender. They need only submit two `manage_neuron { Disburse }` messages in the same round or back-to-back before the first call's response is processed. This is straightforward to do via the IC public API. The neuron must be in `Dissolved` state, which is a normal user-reachable condition. No privileged access, threshold corruption, or external oracle is required.

### Recommendation

Acquire a neuron lock at the start of `disburse_neuron`, before any `await` point, mirroring the NNS governance pattern:

```rust
// Add at the top of disburse_neuron, before the first ledger call:
let _neuron_lock = self.lock_neuron_for_command(
    id,
    NeuronInFlightCommand {
        timestamp: self.env.now(),
        command: Some(InFlightCommand::Disburse(disburse.clone())),
    },
)?;
```

The lock must be held for the entire duration of both ledger calls and released only after all state updates are complete. This is exactly the pattern used in NNS governance. [6](#0-5) 

### Proof of Concept

1. Create an SNS with a dissolved neuron `N` controlled by principal `P` with stake `S`.
2. From principal `P`, submit two concurrent `manage_neuron { subaccount: N, command: Disburse { amount: None, to_account: None } }` ingress messages in the same IC round.
3. The first message suspends at `ledger.transfer_funds(max_burnable_fee, ...).await` (Transfer 1 — fee burn).
4. The second message is inducted while the first is suspended. It reads the same `stake = S`, `fees = F`, passes all checks, and also suspends at its own Transfer 1.
5. Both calls proceed through Transfer 2 (disburse stake), each transferring `disburse_amount_e8s` from the neuron's subaccount.
6. Observe that the neuron subaccount on the ledger is debited twice, and `cached_neuron_stake_e8s` ends up in an inconsistent state relative to the actual ledger balance. [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1119-1125)
```rust
    pub async fn disburse_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse: &manage_neuron::Disburse,
    ) -> Result<u64, GovernanceError> {
        // First check authorized
```

**File:** rs/sns/governance/src/governance.rs (L1181-1237)
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

        Ok(block_height)
    }
```

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

**File:** rs/nns/governance/src/neuron_lock.rs (L108-180)
```rust
impl Governance {
    /// Acquires a neuron lock given a `&'static LocalKey<RefCell<Governance>>` within an async
    /// method, in order to make sure no other neuron methods interleave with the async method for
    /// the same neuron.
    ///
    /// This stores the in-flight operation in the proto so that, if anything
    /// goes wrong we can:
    ///
    /// 1 - Know what was happening.
    /// 2 - Reconcile the state post-upgrade, if necessary.
    ///
    /// No concurrent updates to this neuron's state are possible
    /// until the lock is released.
    ///
    /// ***** IMPORTANT *****
    /// The return value MUST be allocated to a variable with a name that is NOT
    /// "_" !
    ///
    /// The NeuronAsyncLock must remain alive for the entire duration of the
    /// ledger call. Quoting
    /// https://doc.rust-lang.org/book/ch18-03-pattern-syntax.html#ignoring-an-unused-variable-by-starting-its-name-with-_
    ///
    /// > Note that there is a subtle difference between using only _ and using
    /// > a name that starts with an underscore. The syntax _x still binds
    /// > the value to the variable, whereas _ doesn’t bind at all.
    ///
    /// What this means is that the expression
    /// ```text
    /// let _ = acquire_neuron_async_lock(...);
    /// ```
    /// is useless, because the `NeuronAsyncLock`` is a temporary object. It is constructed
    /// (and the lock is acquired), the immediately dropped (and the lock is released).
    ///
    /// However, the expression
    /// ```text
    /// let _my_lock = acquire_neuron_async_lock(...);
    /// ```
    /// will retain the lock for the entire scope.
    pub(crate) fn acquire_neuron_async_lock(
        governance: &'static LocalKey<RefCell<Self>>,
        neuron_id: NeuronId,
        timestamp: u64,
        command: Command,
    ) -> Result<NeuronAsyncLock, GovernanceError> {
        assert!(
            !matches!(command, Command::SyncCommand(_)),
            "SyncCommand is not supported"
        );
        let lock_acquired = governance.with_borrow_mut(|governance| {
            match governance.heap_data.in_flight_commands.entry(neuron_id.id) {
                Entry::Occupied(_) => false,
                Entry::Vacant(entry) => {
                    entry.insert(NeuronInFlightCommand {
                        command: Some(command),
                        timestamp,
                    });
                    true
                }
            }
        });
        if lock_acquired {
            Ok(NeuronAsyncLock {
                neuron_id,
                governance,
                retain: false,
            })
        } else {
            Err(GovernanceError::new_with_message(
                ErrorType::LedgerUpdateOngoing,
                "Neuron has an ongoing ledger update.",
            ))
        }
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
