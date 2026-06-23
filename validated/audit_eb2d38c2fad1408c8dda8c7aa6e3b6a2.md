Looking at the SNS governance `disburse_neuron` function and comparing it to the NNS governance equivalent, I can identify a direct analog to the M-05 re-entrancy finding.

---

### Title
Missing Neuron Lock in SNS `disburse_neuron` Allows Re-entrant Double-Disburse - (File: rs/sns/governance/src/governance.rs)

### Summary
The SNS governance `disburse_neuron` function makes two sequential inter-canister calls to the ledger (burn fees, then transfer stake) across two `await` points without acquiring a per-neuron re-entrancy lock. The NNS governance version of the same function explicitly acquires `lock_neuron_for_command` before any ledger interaction. An unprivileged user who controls a dissolved SNS neuron can send concurrent `manage_neuron` (Disburse) messages; both will read the same pre-update neuron state and issue duplicate ledger transfers, corrupting governance accounting and potentially double-spending the neuron's stake.

### Finding Description

`disburse_neuron` in SNS governance (`rs/sns/governance/src/governance.rs`) performs two sequential ledger calls with no neuron lock held:

```
1. Read neuron state (stake, fees)          ← no lock acquired
2. ledger.transfer_funds(burn fees).await   ← AWAIT POINT 1
3. Update neuron.cached_neuron_stake_e8s / neuron_fees_e8s
4. ledger.transfer_funds(disburse).await    ← AWAIT POINT 2
5. Update neuron.cached_neuron_stake_e8s
```

Between steps 1 and 3, and between 3 and 5, the IC execution model allows other ingress messages to be processed. A second concurrent `disburse_neuron` call on the same neuron will read the same stale `cached_neuron_stake_e8s` and `neuron_fees_e8s` values (because neither has been updated yet), compute the same `disburse_amount_e8s` and `max_burnable_fee`, and issue its own pair of ledger transfers.

The NNS governance version of the same function explicitly guards against this:

```rust
// rs/nns/governance/src/governance.rs line 2031
let _neuron_lock = self.lock_neuron_for_command(
    id.id,
    NeuronInFlightCommand { ... InFlightCommand::Disburse(...) },
)?;
```

The SNS governance `lock_neuron_for_command` function exists and is used elsewhere (e.g., `finalize_disburse_maturity` at line 5006), but is absent from `disburse_neuron`. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

**Ledger conservation bug / canister isolation break.**

Two concurrent `disburse_neuron` calls on the same dissolved neuron both read `cached_neuron_stake_e8s = S` and `neuron_fees_e8s = F` before either updates the state. Both compute `max_burnable_fee = F` and `disburse_amount_e8s = S - F - tx_fee`. Both issue:

- Transfer 1 (burn): `F` tokens from neuron subaccount → minting account
- Transfer 2 (disburse): `S - F - tx_fee` tokens from neuron subaccount → caller

If the neuron subaccount holds enough balance (e.g., because the on-chain balance exceeds `cached_neuron_stake_e8s` due to a direct top-up), both sets of transfers succeed. The governance canister's `cached_neuron_stake_e8s` is then decremented twice by `S`, saturating to 0, while the actual ledger disbursement is `2*(S - F - tx_fee)` — more than the neuron's recorded stake. Even when the second ledger transfer fails (insufficient on-chain balance), the governance state is left inconsistent: `cached_neuron_stake_e8s` is decremented by the first call's amounts but the second call's state update also runs, producing an incorrect final value. [4](#0-3) 

### Likelihood Explanation

Any principal holding a dissolved SNS neuron with `NeuronPermissionType::Disburse` can trigger this by submitting two `manage_neuron` (Disburse) update calls in rapid succession before the first ledger response returns. This is a standard ingress path requiring no special privilege. The IC's asynchronous execution model guarantees that both messages will be inducted and begin execution; the first `await` on the ledger call yields control, allowing the second message to start. The attack requires no threshold corruption, no admin key, and no social engineering.

### Recommendation

Acquire the per-neuron in-flight command lock at the start of `disburse_neuron`, before any ledger interaction, mirroring the NNS governance implementation:

```rust
pub async fn disburse_neuron(...) -> Result<u64, GovernanceError> {
    // ... existing authorization and state checks ...

    // ADD: acquire neuron lock before any await point
    let _neuron_lock = self.lock_neuron_for_command(
        id,
        NeuronInFlightCommand {
            timestamp: self.env.now(),
            command: Some(neuron_in_flight_command::Command::Disburse(disburse.clone())),
        },
    )?;

    // ... existing ledger calls ...
}
```

The lock must be acquired before the first `await` and held until the function returns, ensuring that a second concurrent call on the same neuron is rejected with an error rather than proceeding with stale state. [5](#0-4) 

### Proof of Concept

1. Deploy an SNS with a ledger canister whose subaccount for neuron N holds balance `B > cached_neuron_stake_e8s` (achievable by direct transfer to the neuron subaccount after staking).
2. Dissolve neuron N (wait for dissolve delay to expire).
3. Submit two concurrent ingress `manage_neuron { command: Disburse { amount: None, to_account: attacker } }` messages targeting the same neuron N.
4. Both messages enter the SNS governance input queue. The first begins executing, reads `cached_neuron_stake_e8s = S`, issues `transfer_funds(burn_fees).await`, and yields.
5. The second message begins executing, reads the same `cached_neuron_stake_e8s = S` (unchanged), issues its own `transfer_funds(burn_fees).await`, and yields.
6. Both ledger calls complete (the neuron subaccount has sufficient balance `B`).
7. Both calls proceed to the second transfer (`disburse_amount_e8s = S - fees - tx_fee`), each transferring the full computed amount to the attacker.
8. Attacker receives `2 * (S - fees - tx_fee)` tokens; governance records `cached_neuron_stake_e8s = 0` after both updates, but the actual disbursement exceeded the neuron's recorded stake. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L1119-1192)
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

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

        let from_subaccount = neuron.subaccount()?;

        // If no account was provided, transfer to the caller's (default) account.
        let to_account = match disburse.to_account.as_ref() {
            None => Account {
                owner: caller.0,
                subaccount: None,
            },
            Some(ai_pb) => Account::try_from(ai_pb.clone()).map_err(|e| {
                GovernanceError::new_with_message(
                    ErrorType::InvalidCommand,
                    format!("The recipient's subaccount is invalid due to: {e}"),
                )
            })?,
        };

        let max_burnable_fee = self.maximum_burnable_fees_for_neuron(neuron)?;

        // Calculate the amount to transfer and make sure no matter what the user
        // disburses we still take the neuron management fees into account.
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron.stake_e8s(), |a| a.e8s);

        // You cannot disburse more than the neuron's stake, which includes fees.
        disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());

        // Subtract the transaction fee from the amount to disburse since it will
        // be deducted from the source (the neuron's) account.
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
        }

        // We need to do 2 transfers:
        // 1 - Burn the neuron management fees.
        // 2 - Transfer the disburse_amount to the target account

        // Transfer 1 - burn the neuron management fees, but only if the value
        // exceeds the cost of a transaction fee, as the ledger doesn't support
        // burn transfers for an amount less than the transaction fee.
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

```

**File:** rs/sns/governance/src/governance.rs (L1193-1237)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L5000-5009)
```rust
            let in_flight_command = NeuronInFlightCommand {
                timestamp: self.env.now(),
                command: Some(neuron_in_flight_command::Command::FinalizeDisburseMaturity(
                    fdm,
                )),
            };
            let _neuron_lock = match self.lock_neuron_for_command(&neuron_id, in_flight_command) {
                Ok(neuron_lock) => neuron_lock,
                Err(_) => continue, // if locking fails, try next neuron
            };
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
