### Title
Inconsistent `transaction_fee_e8s` Between SNS Governance State and Actual Ledger `transfer_fee` via Independent Proposal Paths - (`rs/sns/governance/src/governance.rs`)

---

### Summary

In the SNS governance canister, the fee value stored in `NervousSystemParameters.transaction_fee_e8s` and the actual `transfer_fee` on the SNS ledger canister can be changed independently via two separate proposal types (`ManageNervousSystemParameters` and `ManageLedgerParameters`). This creates a window where the governance's cached fee diverges from the ledger's enforced fee, causing neuron operations (disburse, stake) that rely on `transaction_fee_e8s_or_panic()` to fail or behave incorrectly.

---

### Finding Description

The SNS system maintains the ledger transfer fee in two places:

1. **`NervousSystemParameters.transaction_fee_e8s`** — stored in the SNS governance canister's state, used by governance logic when computing ledger calls.
2. **The actual `transfer_fee`** — stored in the SNS ICRC-1 ledger canister, enforced on every transfer.

There are two independent proposal paths that can modify these values:

**Path A — `ManageLedgerParameters` proposal** upgrades the ledger with a new `transfer_fee` and, upon confirmed success, also syncs `transaction_fee_e8s` in governance state: [1](#0-0) 

**Path B — `ManageNervousSystemParameters` proposal** directly updates `transaction_fee_e8s` in governance state **without touching the actual ledger fee**: [2](#0-1) 

The `ManageLedgerParameters` struct only carries `transfer_fee`, `token_name`, `token_symbol`, and `token_logo` — it has no mechanism to atomically enforce that `transaction_fee_e8s` in governance matches the ledger fee when changed via `ManageNervousSystemParameters`: [3](#0-2) 

The governance's `transaction_fee_e8s_or_panic()` is used as the authoritative fee value for ledger calls in neuron operations: [4](#0-3) 

The validation of `neuron_minimum_stake_e8s > transaction_fee_e8s` is performed against the governance-cached value, not the actual ledger fee: [5](#0-4) 

---

### Impact Explanation

When `transaction_fee_e8s` in governance diverges from the actual ledger `transfer_fee`:

- **Governance operations that pass `transaction_fee_e8s` as the fee to the ledger will be rejected** with `BadFee` by the ledger, because the ledger enforces its own `transfer_fee`. This can permanently block neuron disbursement for all SNS token holders.
- **The `neuron_minimum_stake_e8s > transaction_fee_e8s` invariant** is enforced only against the governance-cached value. If the actual ledger fee is higher than the cached value, neurons with stake between the two values will fail to disburse.
- **Revenue/fee accounting** in the SNS ecosystem becomes incorrect, as governance computes expected fees using a stale value.

**Impact: Medium** — Neuron disbursement and staking operations can be broken for all SNS participants, not just the proposer.

---

### Likelihood Explanation

Any SNS token holder with sufficient stake can submit a `ManageNervousSystemParameters` proposal to change `transaction_fee_e8s` independently. This can happen accidentally — for example, a community that wants to update governance parameters (e.g., `neuron_minimum_stake_e8s`) might include a `transaction_fee_e8s` change that doesn't match the current ledger fee, or vice versa. Two proposals passed in sequence (one `ManageLedgerParameters`, one `ManageNervousSystemParameters`) can leave the system in an inconsistent state.

**Likelihood: Medium** — Requires a governance proposal to pass, but no malicious intent is needed; accidental divergence is realistic.

---

### Recommendation

1. **Remove `transaction_fee_e8s` from `ManageNervousSystemParameters`** as a directly settable field, or make it read-only (derived from the ledger).
2. **Alternatively**, when `ManageNervousSystemParameters` includes a `transaction_fee_e8s` change, validate that it matches the current ledger fee (queried at proposal execution time).
3. **Enforce atomicity**: any change to `transaction_fee_e8s` in governance state must be accompanied by a corresponding ledger upgrade, as is already done in `perform_manage_ledger_parameters`. [6](#0-5) 

---

### Proof of Concept

1. Deploy an SNS with default `transaction_fee_e8s = 10_000` and ledger `transfer_fee = 10_000`.
2. Pass a `ManageLedgerParameters` proposal setting ledger `transfer_fee = 20_000`. Governance syncs `transaction_fee_e8s = 20_000`.
3. Pass a `ManageNervousSystemParameters` proposal setting `transaction_fee_e8s = 10_000` (without changing the ledger fee). Governance now stores `transaction_fee_e8s = 10_000` while the ledger enforces `transfer_fee = 20_000`.
4. Attempt to disburse a neuron. Governance computes the ledger call using `transaction_fee_e8s_or_panic() = 10_000`, but the ledger rejects it with `BadFee { expected_fee: 20_000 }`.
5. All neuron disbursements are now permanently broken until another governance proposal corrects the mismatch.

The two independent proposal paths are confirmed in production code: [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L3090-3100)
```rust
    async fn perform_manage_ledger_parameters(
        &mut self,
        proposal_id: u64,
        manage_ledger_parameters: ManageLedgerParameters,
    ) -> Result<(), GovernanceError> {
        self.check_no_upgrades_in_progress(Some(proposal_id))?;

        let current_version = self.get_or_reset_deployed_version().await.map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("Could not execute proposal: {err}"),
```

**File:** rs/sns/governance/src/governance.rs (L3189-3196)
```rust
                    // success
                    // update nervous-system-parameters transaction_fee if the fee is changed.
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
                    }
                    return Ok(());
```

**File:** rs/sns/governance/src/governance.rs (L3368-3373)
```rust
    /// Returns the ledger's transaction fee as stored in the service nervous parameters.
    pub(crate) fn transaction_fee_e8s_or_panic(&self) -> u64 {
        self.nervous_system_parameters_or_panic()
            .transaction_fee_e8s
            .expect("NervousSystemParameters must have transaction_fee_e8s")
    }
```

**File:** rs/sns/integration_tests/src/proposals.rs (L186-217)
```rust
            let proposal_payload = Proposal {
                title: "Test valid ManageNervousSystemParameters proposal".into(),
                action: Some(Action::ManageNervousSystemParameters(
                    NervousSystemParameters {
                        transaction_fee_e8s: Some(120_001),
                        neuron_minimum_stake_e8s: Some(398_002_900),
                        ..Default::default()
                    },
                )),
                ..Default::default()
            };

            // Submit a proposal. It should then be executed because the submitter
            // has a majority stake and submitting also votes automatically.
            let proposal_id = sns_canisters
                .make_proposal(&user, &subaccount, proposal_payload)
                .await
                .unwrap();

            let proposal = sns_canisters.get_proposal(proposal_id).await;

            assert_eq!(proposal.action, 2);
            assert_ne!(proposal.decided_timestamp_seconds, 0);
            assert_ne!(proposal.executed_timestamp_seconds, 0);

            let live_sys_params: NervousSystemParameters = sns_canisters
                .governance
                .query_("get_nervous_system_parameters", candid_one, ())
                .await?;

            assert_eq!(live_sys_params.transaction_fee_e8s, Some(120_001));
            assert_eq!(live_sys_params.neuron_minimum_stake_e8s, Some(398_002_900));
```

**File:** rs/sns/governance/src/gen/ic_sns_governance.pb.v1.rs (L558-567)
```rust
pub struct ManageLedgerParameters {
    #[prost(uint64, optional, tag = "1")]
    pub transfer_fee: ::core::option::Option<u64>,
    #[prost(string, optional, tag = "2")]
    pub token_name: ::core::option::Option<::prost::alloc::string::String>,
    #[prost(string, optional, tag = "3")]
    pub token_symbol: ::core::option::Option<::prost::alloc::string::String>,
    #[prost(string, optional, tag = "4")]
    pub token_logo: ::core::option::Option<::prost::alloc::string::String>,
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
