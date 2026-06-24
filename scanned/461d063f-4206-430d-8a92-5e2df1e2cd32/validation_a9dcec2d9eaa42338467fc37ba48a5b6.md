### Title
Unbounded `transfer_fee` in `ManageLedgerParameters` Bypasses Cross-Parameter Invariant, Bricking SNS Ledger and Governance - (`rs/sns/governance/src/proposal.rs`)

---

### Summary

The SNS governance `ManageLedgerParameters` proposal action accepts any arbitrary `transfer_fee` value with no upper bound validation. When executed, it updates the on-chain ledger fee and the `NervousSystemParameters.transaction_fee_e8s` field without checking the required invariant `neuron_minimum_stake_e8s > transaction_fee_e8s`. A governance user with sufficient voting power can set `transfer_fee = u64::MAX`, making all SNS ledger transfers permanently impossible and breaking neuron creation, effectively bricking the SNS.

---

### Finding Description

**Root cause — missing upper-bound validation in proposal validation:**

`validate_and_render_manage_ledger_parameters` in `rs/sns/governance/src/proposal.rs` accepts any `transfer_fee: Option<u64>` value. When `transfer_fee` is `Some(x)`, the function only formats a render string and sets `change = true`. No range check, no upper bound, no cross-parameter consistency check is performed. [1](#0-0) 

Compare this to `token_name`, `token_symbol`, and `token_logo`, which each call dedicated `ledger_validation::validate_*` helpers. The `transfer_fee` field has no equivalent validator. [2](#0-1) 

**Root cause — cross-parameter invariant not checked at execution:**

`perform_manage_ledger_parameters` in `rs/sns/governance/src/governance.rs` upgrades the ledger canister with the new fee, then on success directly writes the new fee into `NervousSystemParameters.transaction_fee_e8s`: [3](#0-2) [4](#0-3) 

This write is unconditional. It does not call `NervousSystemParameters::validate()`, and specifically does not call `validate_neuron_minimum_stake_e8s`, which enforces the invariant:

```
neuron_minimum_stake_e8s > transaction_fee_e8s
``` [5](#0-4) 

This invariant IS enforced when `ManageNervousSystemParameters` proposals are executed: [6](#0-5) 

But the `ManageLedgerParameters` execution path completely bypasses it.

**The `ManageLedgerParameters` struct itself has no bounds:** [7](#0-6) 

`transfer_fee` is a bare `Option<u64>` — the full `[0, u64::MAX]` range is accepted.

---

### Impact Explanation

Setting `transfer_fee = u64::MAX` (or any value ≥ `neuron_minimum_stake_e8s`) via a passed `ManageLedgerParameters` proposal causes:

1. **All SNS ledger transfers permanently fail.** The ICRC-1 ledger enforces the fee on every transfer. With `transfer_fee = u64::MAX`, no user can ever pay the fee, making the entire SNS token non-transferable.

2. **The `neuron_minimum_stake_e8s > transaction_fee_e8s` invariant is silently broken.** After execution, `NervousSystemParameters.transaction_fee_e8s` is set to the new large value while `neuron_minimum_stake_e8s` remains unchanged. Any subsequent attempt to create a new neuron (which checks `balance >= neuron_minimum_stake_e8s`) will succeed the balance check but fail at the ledger transfer step because the fee exceeds the stake. New neuron creation is permanently broken.

3. **The SNS governance is effectively bricked.** With no new neurons creatable and no token transfers possible, the SNS community cannot recover through normal governance (proposals require neuron stake, which requires transfers). The SNS is permanently frozen.

4. **The broken invariant cannot be repaired via `ManageNervousSystemParameters`.** Attempting to lower `transaction_fee_e8s` back via `ManageNervousSystemParameters` would call `validate()`, which checks `neuron_minimum_stake_e8s > transaction_fee_e8s` — but the ledger's actual fee is still `u64::MAX`, so the two are now permanently out of sync.

---

### Likelihood Explanation

The attacker is a **governance/chain-fusion user** — an SNS neuron holder (or coalition) with enough voting power to pass a `ManageLedgerParameters` proposal (Action Id = 13). This is explicitly within the stated attacker scope. The proposal passes standard SNS governance validation because `validate_and_render_manage_ledger_parameters` accepts any fee value. No privileged key, no subnet majority, and no external oracle is required. The attack is a single on-chain governance proposal.

---

### Recommendation

1. **Add an upper bound to `transfer_fee` in `validate_and_render_manage_ledger_parameters`:**

```rust
if let Some(transfer_fee) = transfer_fee {
    // e.g., cap at neuron_minimum_stake_e8s or a protocol-defined maximum
    if *transfer_fee > MAX_TRANSFER_FEE {
        return Err(format!(
            "transfer_fee ({transfer_fee}) exceeds the maximum allowed value ({MAX_TRANSFER_FEE})"
        ));
    }
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n");
    change = true;
}
```

2. **Validate cross-parameter consistency in `perform_manage_ledger_parameters`:** Before writing `transaction_fee_e8s`, check that the new fee is strictly less than the current `neuron_minimum_stake_e8s`.

3. **Pass the current `NervousSystemParameters` into `validate_and_render_manage_ledger_parameters`** so that the proposed fee can be validated against `neuron_minimum_stake_e8s` at proposal submission time, not just at execution time.

---

### Proof of Concept

1. An SNS neuron holder with sufficient voting power submits:
   ```
   ManageLedgerParameters { transfer_fee: Some(u64::MAX), .. }
   ```

2. `validate_and_render_manage_ledger_parameters` is called. It finds `transfer_fee = Some(u64::MAX)`, sets `change = true`, and returns `Ok(render)` — no error. [1](#0-0) 

3. The proposal passes governance voting and `perform_manage_ledger_parameters` is called. The ledger is upgraded with `transfer_fee = u64::MAX`. [8](#0-7) 

4. On confirmed upgrade success, `transaction_fee_e8s` is set to `u64::MAX` in `NervousSystemParameters` with no invariant check: [4](#0-3) 

5. All subsequent ICRC-1 transfers fail with `BadFee { expected_fee: u64::MAX }`. Neuron creation is impossible. The SNS is bricked.

### Citations

**File:** rs/sns/governance/src/proposal.rs (L1773-1776)
```rust
    if let Some(transfer_fee) = transfer_fee {
        render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n",);
        change = true;
    }
```

**File:** rs/sns/governance/src/proposal.rs (L1777-1791)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L1190-1195)
```rust
                )
                .await?;

            // We only update the cached_neuron_stake_e8s and neuron_fees_e8s if we actually
            // burn fees, otherwise this leads to ledger and governance getting out of sync.
            let nid = id.to_string();
```

**File:** rs/sns/governance/src/governance.rs (L2579-2600)
```rust
    /// Executes a ManageNervousSystemParameters proposal by updating Governance's
    /// NervousSystemParameters
    fn perform_manage_nervous_system_parameters(
        &mut self,
        proposed_params: NervousSystemParameters,
    ) -> Result<(), GovernanceError> {
        // Only set `self.proto.parameters` if "applying" the proposed params to the
        // current params results in valid params
        let new_params = proposed_params.inherit_from(self.nervous_system_parameters_or_panic());

        log!(
            INFO,
            "Setting Governance nervous system params to: {:?}",
            &new_params
        );

        match new_params.validate() {
            Ok(()) => {
                self.proto.parameters = Some(new_params);
                Ok(())
            }

```

**File:** rs/sns/governance/src/governance.rs (L3144-3156)
```rust
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

**File:** rs/sns/governance/src/governance.rs (L3191-3195)
```rust
                    if let Some(nervous_system_parameters) = self.proto.parameters.as_mut()
                        && let Some(transfer_fee) = manage_ledger_parameters.transfer_fee
                    {
                        nervous_system_parameters.transaction_fee_e8s = Some(transfer_fee);
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
