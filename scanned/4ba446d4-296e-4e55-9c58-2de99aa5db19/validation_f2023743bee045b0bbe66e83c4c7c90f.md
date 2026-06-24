### Title
SNS Governance `ManageLedgerParameters` Can Set `transfer_fee` Above `neuron_minimum_stake_e8s`, Permanently Blocking Neuron Disbursal and Split - (`rs/sns/governance/src/proposal.rs`)

---

### Summary

The SNS governance `ManageLedgerParameters` proposal action allows the SNS community to raise the ledger `transfer_fee` to an arbitrary value with no cross-validation against `NervousSystemParameters.neuron_minimum_stake_e8s`. When `transfer_fee` is raised above `neuron_minimum_stake_e8s`, the core invariant `neuron_minimum_stake_e8s > transaction_fee_e8s` is silently broken. Existing neuron holders whose stake falls between the old minimum and the new fee can no longer disburse or split their neurons — their staked tokens become permanently inaccessible through normal governance operations.

---

### Finding Description

`ManageLedgerParameters` is a governance proposal action that changes the SNS ledger's `transfer_fee` and simultaneously updates `NervousSystemParameters.transaction_fee_e8s` to match. [1](#0-0) 

The validation function `validate_and_render_manage_ledger_parameters` accepts any `transfer_fee` value — it only checks that at least one field is non-`None`. There is no check that the new fee is less than `neuron_minimum_stake_e8s`: [2](#0-1) 

By contrast, `NervousSystemParameters.validate_neuron_minimum_stake_e8s` enforces the invariant `neuron_minimum_stake_e8s > transaction_fee_e8s`, but this check is only invoked when a `ManageNervousSystemParameters` proposal is validated — never when `ManageLedgerParameters` executes: [3](#0-2) 

When `ManageLedgerParameters` executes and sets `transfer_fee = X`, `NervousSystemParameters.transaction_fee_e8s` is updated to `X` as confirmed by the integration test: [4](#0-3) 

After this update, if `X >= neuron_minimum_stake_e8s`, the invariant is broken. The `disburse_neuron` function in SNS governance reads `transaction_fee_e8s` from `NervousSystemParameters` and passes it to the ledger: [5](#0-4) 

If `transaction_fee_e8s >= neuron.stake_e8s()`, the ledger transfer call will fail with an insufficient-funds error, permanently blocking disbursal. Similarly, `split_neuron` enforces `split_amount_e8s >= min_stake + transaction_fee_e8s`: [6](#0-5) 

When `transaction_fee_e8s > neuron_minimum_stake_e8s`, this condition can never be satisfied for any split amount, making `split_neuron` permanently revert for all neurons at or near the minimum stake.

The proto comment for `ManageNervousSystemParameters` explicitly acknowledges that parameter changes do not retroactively affect existing neurons: [7](#0-6) 

But `ManageLedgerParameters` has no such acknowledgment or guard, and its effect on `transaction_fee_e8s` is immediate and retroactive.

---

### Impact Explanation

After a `ManageLedgerParameters` proposal raises `transfer_fee` above `neuron_minimum_stake_e8s`:

1. **Disburse blocked**: `disburse_neuron` calls the ledger with `transaction_fee_e8s` equal to the new high fee; the ledger rejects the transfer for neurons whose stake is below the fee. Staked tokens are permanently inaccessible.
2. **Split blocked**: `split_neuron` requires `split_amount_e8s >= min_stake + transaction_fee_e8s`; when `transaction_fee_e8s > neuron_minimum_stake_e8s`, no valid split amount exists for small neurons.
3. **Refresh blocked**: `refresh_neuron` checks `balance >= neuron_minimum_stake_e8s`, but the ledger account balance is also reduced by the fee on any top-up transfer, making it impossible to bring a small neuron back above the minimum. [8](#0-7) 

The severity is high: user funds (SNS tokens staked in neurons) become permanently locked with no recovery path available through the governance canister's public API.

---

### Likelihood Explanation

Any SNS neuron holder with sufficient stake to submit a proposal can trigger this. The proposal requires a governance majority to pass, but the action is not inherently malicious — an SNS community might legitimately raise the transfer fee for economic reasons (e.g., to reduce spam) without realizing the impact on small neuron holders. The lack of any validation or warning in `validate_and_render_manage_ledger_parameters` means the side effect is invisible at proposal submission time. SNS communities with many small-stake neurons (e.g., from decentralization swaps with many participants) are particularly exposed. [9](#0-8) 

---

### Recommendation

`validate_and_render_manage_ledger_parameters` should cross-validate the proposed `transfer_fee` against the current `neuron_minimum_stake_e8s` from `NervousSystemParameters`, rejecting any proposal where `transfer_fee >= neuron_minimum_stake_e8s`. Alternatively, `ManageLedgerParameters` execution should atomically update `neuron_minimum_stake_e8s` to maintain the invariant, and should scan existing neurons to warn or block if any would be rendered non-disbursable. At minimum, the proposal render text should explicitly warn that neurons with stake below the new fee will be unable to disburse.

---

### Proof of Concept

1. Deploy an SNS with default parameters: `neuron_minimum_stake_e8s = 100_000_000`, `transaction_fee_e8s = 10_000`.
2. User A stakes `110_000` tokens and claims a neuron (above the minimum stake of `100_000`).
3. SNS governance passes a `ManageLedgerParameters` proposal setting `transfer_fee = 200_000`.
4. `NervousSystemParameters.transaction_fee_e8s` is now `200_000`.
5. User A's neuron has `stake_e8s() = 110_000 - neuron_fees = ~110_000`.
6. User A calls `disburse_neuron`: governance computes `disburse_amount_e8s = stake_e8s() = 110_000`, then since `110_000 > 200_000` is false, `disburse_amount_e8s` stays at `110_000`. The ledger transfer call passes `fee = 200_000` but the neuron account only holds `110_000` tokens — the ledger rejects with insufficient funds.
7. User A calls `split_neuron` with any amount: `split_amount_e8s >= min_stake(100_000) + transaction_fee_e8s(200_000) = 300_000` is required, but the neuron only has `110_000` stake — permanently blocked. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/sns/governance/src/proposal.rs (L1761-1799)
```rust
fn validate_and_render_manage_ledger_parameters(
    manage_ledger_parameters: &ManageLedgerParameters,
) -> Result<String, String> {
    let mut change = false;
    let mut render = "# Proposal to change ledger parameters:\n".to_string();
    let ManageLedgerParameters {
        transfer_fee,
        token_name,
        token_symbol,
        token_logo,
    } = manage_ledger_parameters;

    if let Some(transfer_fee) = transfer_fee {
        render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
        change = true;
    }
    if let Some(token_name) = token_name {
        ledger_validation::validate_token_name(token_name)?;
        render += &format!("# Set token name: {token_name}. \n",);
        change = true;
    }
    if let Some(token_symbol) = token_symbol {
        ledger_validation::validate_token_symbol(token_symbol)?;
        render += &format!("# Set token symbol: {token_symbol}. \n",);
        change = true;
    }
    if let Some(token_logo) = token_logo {
        ledger_validation::validate_token_logo(token_logo)?;
        render += &format!("# Set token logo: {token_logo}. \n",);
        change = true;
    }
    if !change {
        Err(String::from(
            "ManageLedgerParameters must change at least one value, all values are None",
        ))
    } else {
        Ok(render)
    }
}
```

**File:** rs/sns/governance/src/types.rs (L602-618)
```rust
    /// Validates that the nervous system parameter neuron_minimum_stake_e8s is well-formed.
    fn validate_neuron_minimum_stake_e8s(&self) -> Result<(), String> {
        let transaction_fee_e8s = self.validate_transaction_fee_e8s()?;

        let neuron_minimum_stake_e8s = self.neuron_minimum_stake_e8s.ok_or_else(|| {
            "NervousSystemParameters.neuron_minimum_stake_e8s must be set".to_string()
        })?;

        if neuron_minimum_stake_e8s <= transaction_fee_e8s {
            Err(format!(
                "NervousSystemParameters.neuron_minimum_stake_e8s ({neuron_minimum_stake_e8s}) must be greater than \
                NervousSystemParameters.transaction_fee_e8s ({neuron_minimum_stake_e8s})"
            ))
        } else {
            Ok(())
        }
    }
```

**File:** rs/sns/integration_tests/src/manage_ledger_parameters.rs (L120-135)
```rust
    let nervous_system_parameters_with_new_fee: NervousSystemParameters = {
        let nervous_system_parameters_raw = query(
            &state_machine,
            sns_canisters.governance_canister_id,
            "get_nervous_system_parameters",
            Encode!().unwrap(),
        )
        .unwrap();

        candid::decode_one(&nervous_system_parameters_raw).unwrap()
    };

    assert_eq!(
        nervous_system_parameters_with_new_fee.transaction_fee_e8s,
        Some(new_fee)
    );
```

**File:** rs/sns/governance/src/governance.rs (L1119-1172)
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
```

**File:** rs/sns/governance/src/governance.rs (L1292-1331)
```rust
    pub async fn split_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        split: &manage_neuron::Split,
    ) -> Result<NeuronId, GovernanceError> {
        // New neurons are not allowed when the heap is too large.
        self.check_heap_can_grow()?;

        let min_stake = self
            .proto
            .parameters
            .as_ref()
            .expect("Governance must have NervousSystemParameters.")
            .neuron_minimum_stake_e8s
            .expect("NervousSystemParameters must have neuron_minimum_stake_e8s");

        let transaction_fee_e8s = self.transaction_fee_e8s_or_panic();

        // Get the neuron and clone to appease the borrow checker.
        // We'll get a mutable reference when we need to change it later.
        let parent_neuron = self.get_neuron_result(id)?.clone();
        let parent_nid = parent_neuron.id.as_ref().expect("Neurons must have an id");

        parent_neuron.check_authorized(caller, NeuronPermissionType::Split)?;

        if split.amount_e8s < min_stake + transaction_fee_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::InsufficientFunds,
                format!(
                    "Trying to split a neuron with argument {} e8s. This is too little: \
                      at the minimum, one needs the minimum neuron stake, which is {} e8s, \
                      plus the transaction fee, which is {}. Hence the minimum split amount is {}.",
                    split.amount_e8s,
                    min_stake,
                    transaction_fee_e8s,
                    min_stake + transaction_fee_e8s
                ),
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L4258-4272)
```rust
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
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L648-658)
```text
    // Change the nervous system's parameters.
    // Note that a change of a parameter will only affect future actions where
    // this parameter is relevant.
    // For example, NervousSystemParameters::neuron_minimum_stake_e8s specifies the
    // minimum amount of stake a neuron must have, which is checked at the time when
    // the neuron is created. If this NervousSystemParameter is decreased, all neurons
    // created after this change will have at least the new minimum stake. However,
    // neurons created before this change may have less stake.
    //
    // Id = 2.
    NervousSystemParameters manage_nervous_system_parameters = 6;
```
