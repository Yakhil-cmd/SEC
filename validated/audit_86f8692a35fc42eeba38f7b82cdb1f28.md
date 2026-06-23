### Title
Missing On-Chain Bounds Validation for `transfer_fee` in SNS `ManageLedgerParameters` Proposal - (`File: rs/sns/governance/src/proposal.rs`)

### Summary
The SNS governance canister's `validate_and_render_manage_ledger_parameters` function accepts any arbitrary `transfer_fee` value in a `ManageLedgerParameters` proposal without enforcing minimum or maximum bounds. A malicious or negligent SNS community that passes such a proposal can set the ledger transfer fee to `u64::MAX`, permanently bricking all token transfers for every holder of that SNS token, or set it to `0`, eliminating fee revenue entirely. This is a direct on-chain governance authorization bug with a concrete, irreversible financial and operational impact.

### Finding Description

The `validate_and_render_manage_ledger_parameters` function in `rs/sns/governance/src/proposal.rs` is the sole validation gate for the `ManageLedgerParameters` proposal action. When `transfer_fee` is `Some(value)`, the function only renders a human-readable string and sets a `change = true` flag — it performs **no numeric validation** on the fee value itself. [1](#0-0) 

By contrast, the other fields in the same function (`token_name`, `token_symbol`, `token_logo`) each call dedicated validation helpers (`ledger_validation::validate_token_name`, etc.) that enforce length and format constraints. [2](#0-1) 

When the proposal is executed, `perform_manage_ledger_parameters` converts the `ManageLedgerParameters` directly into `LedgerUpgradeArgs` and upgrades the SNS ledger canister with the unchecked fee value. [3](#0-2) 

The conversion path is:

```
ManageLedgerParameters { transfer_fee: Some(u64::MAX) }
  → LedgerUpgradeArgs::from(manage_ledger_parameters)   [rs/sns/governance/src/types.rs:2017-2038]
  → SNS ledger canister upgrade with fee = u64::MAX
``` [4](#0-3) 

The SNS `NervousSystemParameters` does enforce that `neuron_minimum_stake_e8s > transaction_fee_e8s` at init/upgrade time, but this cross-parameter invariant is **not re-checked** when `ManageLedgerParameters` changes the ledger fee independently of the governance parameters. [5](#0-4) 

The `ManageLedgerParameters` proto definition confirms `transfer_fee` is a bare `optional uint64` with no documented bounds. [6](#0-5) 

### Impact Explanation

**Scenario A — Fee set to `u64::MAX`:** Every `icrc1_transfer` call on the SNS ledger will fail with `InsufficientFunds` because no account can hold `u64::MAX` tokens. All token holders are permanently unable to transfer, stake, or disburse their tokens. Neurons cannot be staked or split. The SNS is effectively frozen.

**Scenario B — Fee set to `0`:** The SNS ledger charges no fee for transfers. This eliminates the fee-burn/fee-collection mechanism, breaking the economic model of the SNS and any integrators relying on fee revenue.

**Scenario C — Fee set above `neuron_minimum_stake_e8s`:** The invariant `neuron_minimum_stake_e8s > transaction_fee_e8s` (enforced at init) is silently violated. Neuron split and disburse operations that rely on `min_stake + transaction_fee_e8s` arithmetic will behave incorrectly or revert, breaking governance participation. [7](#0-6) 

### Likelihood Explanation

The attacker-controlled entry path is a standard SNS governance proposal submitted by any neuron holder with sufficient voting power. The `ManageLedgerParameters` action (id=13) is a native SNS action reachable by any unprivileged principal who controls a neuron. No admin key, leaked secret, or privileged access is required. A malicious neuron holder, or a coalition that reaches voting threshold, can submit and pass this proposal. The risk is also present from accidental misconfiguration (e.g., off-by-one in e8s units).

### Recommendation

Add numeric bounds validation inside `validate_and_render_manage_ledger_parameters` for `transfer_fee`:

1. Reject `transfer_fee == 0` (or enforce a protocol-defined minimum, e.g., `1`).
2. Enforce a maximum cap (e.g., `u64::MAX / 2` or a domain-appropriate ceiling).
3. Cross-validate that the proposed `transfer_fee` does not exceed the current `NervousSystemParameters.neuron_minimum_stake_e8s`, preserving the invariant that `neuron_minimum_stake_e8s > transaction_fee_e8s`.

The analogous pattern already exists in `NervousSystemParameters::validate_neuron_minimum_stake_e8s`: [8](#0-7) 

### Proof of Concept

1. An SNS neuron holder submits a `ManageLedgerParameters` proposal with `transfer_fee = Some(u64::MAX)`.
2. The proposal passes `validate_and_render_manage_ledger_parameters` — the only validation is `change = true` (line 1775); no numeric check is performed.
3. The proposal is adopted by the SNS community (or by a malicious majority).
4. `perform_manage_ledger_parameters` is called, which calls `LedgerUpgradeArgs::from(manage_ledger_parameters)` and upgrades the SNS ledger with `transfer_fee = u64::MAX`.
5. All subsequent `icrc1_transfer` calls on the SNS ledger return `BadFee { expected_fee: u64::MAX }` or `InsufficientFunds`, permanently bricking token transfers for all holders. [9](#0-8) [10](#0-9)

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

**File:** rs/sns/governance/src/governance.rs (L1318-1330)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L3090-3156)
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
            )
        })?;

        let ledger_canister_id = self.proto.ledger_canister_id_or_panic();

        let ledger_canister_info = self.env
            .call_canister(
                CanisterId::ic_00(),
                "canister_info",
                candid::encode_one(
                    CanisterInfoRequest::new(
                        ledger_canister_id,
                        Some(1),
                    )
                ).map_err(|e| GovernanceError::new_with_message(ErrorType::External, format!("Could not execute proposal. Error encoding canister_info request.\n{e}")))?
            )
            .await
            .map(|b| {
                candid::decode_one::<CanisterInfoResponse>(&b)
                .map_err(|e| GovernanceError::new_with_message(ErrorType::External, format!("Could not execute proposal. Error decoding canister_info response.\n{e}")))
            })
            .map_err(|err: (Option<i32>, String)| GovernanceError::new_with_message(ErrorType::External, format!("Canister method call canister_info failed: {err:?}")))??;

        let ledger_canister_info_version_number_before_upgrade: u64 =
            ledger_canister_info
            .changes()
            .last().ok_or(GovernanceError::new_with_message(ErrorType::External, "Could not execute proposal. Error finding current ledger canister_info version number".to_string()))?
            .canister_version();

        let ledger_wasm = get_wasm(
            &*self.env,
            current_version.ledger_wasm_hash.clone(),
            SnsCanisterType::Ledger,
        )
        .await
        .map_err(|e| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("Could not execute proposal. Error getting ledger canister wasm: {e}"),
            )
        })?
        .wasm;

        use ic_icrc1_ledger::{LedgerArgument, UpgradeArgs};
        let ledger_upgrade_arg = candid::encode_one(Some(LedgerArgument::Upgrade(Some(
            UpgradeArgs::from(manage_ledger_parameters.clone()),
        ))))
        .unwrap();

        self.upgrade_non_root_canister(
            ledger_canister_id,
            Wasm::Bytes(ledger_wasm),
            ledger_upgrade_arg,
            CanisterInstallMode::Upgrade,
        )
        .await?;
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

**File:** rs/sns/governance/src/types.rs (L2017-2038)
```rust
impl From<ManageLedgerParameters> for LedgerUpgradeArgs {
    fn from(manage_ledger_parameters: ManageLedgerParameters) -> Self {
        let ManageLedgerParameters {
            transfer_fee,
            token_name,
            token_symbol,
            token_logo,
        } = manage_ledger_parameters;

        let metadata = token_logo.map(|token_logo| {
            let value = MetadataValue::Text(token_logo);
            vec![(MetadataKey::ICRC1_LOGO.to_string(), value)]
        });

        LedgerUpgradeArgs {
            transfer_fee: transfer_fee.map(|tf| tf.into()),
            token_name,
            token_symbol,
            metadata,
            ..LedgerUpgradeArgs::default()
        }
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L393-400)
```text
// A proposal function that changes the ledger's parameters.
// Fields with None values will remain unchanged.
message ManageLedgerParameters {
  optional uint64 transfer_fee = 1;
  optional string token_name = 2;
  optional string token_symbol = 3;
  optional string token_logo = 4;
}
```
