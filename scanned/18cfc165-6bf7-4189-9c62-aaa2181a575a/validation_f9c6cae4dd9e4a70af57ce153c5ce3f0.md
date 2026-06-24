### Title
Missing Neuron Lock Before Inter-Canister Await in SNS Governance `refresh_neuron` Allows Stale Stake Overwrite - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS Governance `refresh_neuron` function makes an inter-canister call to the ledger (`account_balance`) **without first acquiring a neuron lock**. Because IC canisters process other messages between `await` points, a concurrent neuron operation (e.g., `disburse`) can complete and mutate the neuron's cached stake while `refresh_neuron` is suspended. When `refresh_neuron` resumes, it overwrites the neuron's `cached_neuron_stake_e8s` with a stale, pre-operation balance. The NNS Governance implementation of the same function correctly acquires a lock before the await; the SNS implementation does not.

### Finding Description

**SNS `refresh_neuron` — no lock before await (vulnerable):** [1](#0-0) 

The function proceeds directly to the inter-canister call with no lock:

```rust
async fn refresh_neuron(&mut self, nid: &NeuronId) -> Result<(), GovernanceError> {
    // ... neuron_fund check only, no lock acquired ...
    let balance = self.ledger.account_balance(account).await?;  // ← await with no lock
    // state written after await:
    neuron.update_stake(balance.get_e8s(), now);
``` [2](#0-1) 

**NNS `refresh_neuron` — lock acquired before await (correct):** [3](#0-2) 

```rust
let _neuron_lock = self.lock_neuron_for_command(
    nid.id,
    NeuronInFlightCommand { ... },
)?;
// Only then:
let balance = self.ledger.account_balance(account).await?;
```

The lock mechanism itself is well-defined and used correctly elsewhere: [4](#0-3) 

### Impact Explanation

**Interleaving scenario (no attacker required — any concurrent caller suffices):**

1. Neuron N has `cached_neuron_stake_e8s = 100`, actual ledger balance = 100.
2. Call A: `refresh_neuron(N)` — sends `account_balance` request to ledger. **No lock held.**
3. Call B: `disburse(N)` — acquires neuron lock (succeeds, since A holds none), reduces `cached_neuron_stake_e8s` to 0, transfers 100 tokens out of the neuron subaccount, releases lock.
4. Ledger processes in order: `account_balance` → returns 100 (pre-disburse snapshot); then `transfer` → balance becomes 0.
5. Call A resumes with `balance = 100`, writes `cached_neuron_stake_e8s = 100`.

**Result:** `cached_neuron_stake_e8s = 100` but actual ledger balance = 0. The neuron's cached stake is permanently inflated relative to its real on-chain balance. This corrupts voting-power accounting (SNS voting power is proportional to `cached_neuron_stake_e8s`) and can cause subsequent governance operations to behave incorrectly. A user who can trigger `refresh_neuron` on any neuron (the call is permissionless via `ClaimOrRefresh`) can race it against the neuron owner's own `disburse` to leave the neuron in an inconsistent state. [5](#0-4) 

### Likelihood Explanation

- `refresh_neuron` is reachable by any unprivileged ingress sender via the `manage_neuron` → `ClaimOrRefresh` path.
- The race window is the full round-trip latency of an `account_balance` call to the SNS ledger — typically one or more consensus rounds, giving ample time for a concurrent `disburse` or `split` to complete.
- No special privileges, keys, or subnet-majority corruption are required.
- The NNS governance team already identified this exact risk and added the lock; the SNS implementation was not updated consistently. [6](#0-5) 

### Recommendation

Acquire a neuron lock in SNS `refresh_neuron` before the `account_balance` inter-canister call, mirroring the NNS implementation:

```rust
async fn refresh_neuron(&mut self, nid: &NeuronId) -> Result<(), GovernanceError> {
    // ... existing neuron_fund check ...

    // ADD: acquire lock before await
    let _neuron_lock = self.lock_neuron_for_command(
        nid,
        NeuronInFlightCommand { timestamp: now, command: Some(...ClaimOrRefresh...) },
    )?;

    let balance = self.ledger.account_balance(account).await?;
    // ... rest unchanged ...
}
``` [7](#0-6) 

### Proof of Concept

1. Deploy an SNS with a neuron N holding 100 tokens.
2. Submit two concurrent ingress messages to SNS Governance:
   - Message A: `manage_neuron` → `ClaimOrRefresh` → `By::NeuronId` (triggers `refresh_neuron`)
   - Message B: `manage_neuron` → `Disburse` (triggers `disburse`, which acquires the lock and transfers tokens)
3. Using PocketIC's `submit_call` / `await_call` API (which supports concurrent update calls), submit both messages before either is executed.
4. After both complete, query the neuron: `cached_neuron_stake_e8s` will be 100 while the actual ledger subaccount balance is 0, demonstrating the stale overwrite. [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L865-894)
```rust
    /// Locks a given neuron, signaling there is an ongoing neuron operation.
    ///
    /// This stores the in-flight operation in the proto so that, if anything
    /// goes wrong we can:
    ///
    /// 1 - Know what was happening.
    /// 2 - Reconcile the state post-upgrade, if necessary.
    ///
    /// No concurrent updates that also acquire a lock to this neuron are possible
    /// until the lock is released.
    ///
    /// ***** IMPORTANT *****
    /// Remember to use the question mark operator (or otherwise handle
    /// Err). Otherwise, failed attempts to acquire will be ignored.
    ///
    /// The return value MUST be allocated to a variable with a name that is NOT
    /// "_" !
    ///
    /// The LedgerUpdateLock must remain alive for the entire duration of the
    /// ledger call. Quoting
    /// https://doc.rust-lang.org/book/ch18-03-pattern-syntax.html#ignoring-an-unused-variable-by-starting-its-name-with-_
    ///
    /// > Note that there is a subtle difference between using only _ and using
    /// > a name that starts with an underscore. The syntax _x still binds
    /// > the value to the variable, whereas _ doesn't bind at all.
    ///
    /// What this means is that the expression
    /// ```text
    /// let _ = lock_neuron_for_command(...);
    /// ```
```

**File:** rs/sns/governance/src/governance.rs (L4203-4227)
```rust
    /// Creates a new neuron or refreshes the stake of an existing
    /// neuron from a ledger account.
    /// The neuron id of the neuron to refresh or claim is computed
    /// with the given controller (if none is given the caller is taken)
    /// and the given memo.
    /// If the neuron id exists, the neuron is refreshed and if the neuron id
    /// does not yet exist, the neuron is claimed.
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

**File:** rs/sns/governance/src/governance.rs (L4237-4295)
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

        let min_stake = self
            .nervous_system_parameters_or_panic()
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");
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

**File:** rs/nns/governance/src/neuron_lock.rs (L1-21)
```rust
//! This module defines mechanisms for locking neurons in order to prevent problematic interleaving
//! of neuron operations.
//!
//! The `LedgerUpdateLock` is a legacy mechanism, where the lock contains a `*mut Governance`
//! pointer. An unsafe block is needed to unlock the neuron. In addition, the pointer needs to be
//! `'static` in order for the lock to be used in async contexts. However, using `&'static mut` to
//! access global state is dangerous and should be avoided.
//!
//! The `NeuronAsyncLock` is a new mechanism that uses a `&'static LocalKey<RefCell<Governance>>` to
//! access the global state. This allows for safe access to the global state in async contexts.
//!
//! For sync methods, there is actually no need to acquire the lock, since it's impossible for the
//! lock to be persisted in any case anyway. In the future, a new method on the `Governance` struct
//! can be used to check whether a lock is held for a neuron. However, currently, in order to avoid
//! introducing a 3rd pattern for locking neurons, the recommendation is to keep using
//! `lock_neuron_for_command` with a `SyncCommand`.
//!
//! Note that it's OK for `NeuronAsyncLock` and `LedgerUpdateLock` to co-exist. If a
//! `NeuronAsyncLock` is held for a neuron, and another method tries to acquire a `LedgerUpdateLock`
//! for the same neuron, it will still fail as expected, and vice versa, since their underlying
//! storage is the same `in_flight_commands` map.
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
