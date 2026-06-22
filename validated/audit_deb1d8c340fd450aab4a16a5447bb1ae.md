### Title
No Upper Bound on SNS Ledger `transfer_fee` in `ManageLedgerParameters` Proposal Validation - (File: rs/sns/governance/src/proposal.rs)

### Summary
The SNS governance `ManageLedgerParameters` proposal action accepts an arbitrary `u64` value for `transfer_fee` with no upper bound enforced at the protocol level. A governance user (SNS neuron holder) with sufficient voting power can pass a proposal setting `transfer_fee` to `u64::MAX`, permanently freezing all SNS token transfers for every user.

### Finding Description
`ManageLedgerParameters.transfer_fee` is typed as `Option<u64>`, accepting any value in the range `[0, 18_446_744_073_709_551_615]`. [1](#0-0) 

The proposal validation function `validate_and_render_manage_ledger_parameters` in `rs/sns/governance/src/proposal.rs` only checks that the field is `Some(...)` — it performs no range or reasonableness check on the fee value: [2](#0-1) 

Compare this to the validation applied to `token_name`, `token_symbol`, and `token_logo`, each of which calls a dedicated `ledger_validation::validate_*` function. No equivalent `validate_transfer_fee` guard exists. [3](#0-2) 

Upon proposal execution, `perform_manage_ledger_parameters` in `rs/sns/governance/src/governance.rs` directly converts the raw `ManageLedgerParameters` into `UpgradeArgs` and upgrades the ledger canister with no additional fee validation: [4](#0-3) 

The ICRC1 ledger's `upgrade` function in `rs/ledger_suite/icrc1/ledger/src/lib.rs` then applies the fee unconditionally — the only check is a type conversion, not a value bound: [5](#0-4) 

### Impact Explanation
Setting `transfer_fee` to `u64::MAX` (or any value exceeding the total token supply) makes every `icrc1_transfer`, `icrc2_transfer_from`, and `icrc2_approve` call fail with `BadFee` or `InsufficientFunds`, because no account can hold enough tokens to cover the fee. All SNS token holders are effectively frozen: they cannot transfer, approve, or interact with any protocol that requires a transfer. Neuron staking, unstaking, and disbursal flows that rely on ledger transfers are also disrupted. The impact is a complete denial-of-service on the SNS token economy, causing direct financial harm to all token holders.

### Likelihood Explanation
SNS governance voting power is often highly concentrated at launch (founding team, seed investors). A single entity controlling a simple majority of voting power — a realistic scenario for many early-stage SNS DAOs — can unilaterally pass a `ManageLedgerParameters` proposal with an unbounded fee. No secondary protocol-level check prevents execution. Likelihood is low-to-medium: it requires governance majority, but that bar is achievable for a motivated actor in a concentrated SNS.

### Recommendation
Add an explicit upper bound check inside `validate_and_render_manage_ledger_parameters` in `rs/sns/governance/src/proposal.rs`. A reasonable maximum (e.g., capped at the total token supply, or a hard-coded protocol maximum such as `1_000_000_000_000` e8s) should be enforced before the proposal is accepted. The same guard should be mirrored in the ICRC1 ledger's `upgrade` function in `rs/ledger_suite/icrc1/ledger/src/lib.rs` as a defense-in-depth measure.

```rust
// In validate_and_render_manage_ledger_parameters
if let Some(transfer_fee) = transfer_fee {
    const MAX_TRANSFER_FEE: u64 = 1_000_000_000_000; // example upper bound
    if *transfer_fee > MAX_TRANSFER_FEE {
        return Err(format!(
            "transfer_fee {transfer_fee} exceeds the maximum allowed value {MAX_TRANSFER_FEE}"
        ));
    }
    render += &format!("# Set token transfer fee: {transfer_fee} token-quantums. \n");
    change = true;
}
```

### Proof of Concept
1. Deploy an SNS with a standard ICRC1 ledger and `transfer_fee = 10_000`.
2. As a neuron holder with majority voting power, submit a `ManageLedgerParameters` proposal:
   ```
   ManageLedgerParameters {
       transfer_fee: Some(18_446_744_073_709_551_615), // u64::MAX
       ..Default::default()
   }
   ```
3. The proposal passes `validate_and_render_manage_ledger_parameters` without error (only `Some(...)` is checked).
4. Upon execution, `perform_manage_ledger_parameters` upgrades the ledger with `transfer_fee = u64::MAX`.
5. All subsequent `icrc1_transfer` calls return `BadFee { expected_fee: u64::MAX }` or `InsufficientFunds`, since no account balance can cover the fee.
6. All SNS token holders are permanently frozen until a corrective governance proposal is passed — which itself may be impossible if neuron disbursal requires ledger transfers.

### Citations

**File:** rs/sns/governance/api/src/ic_sns_governance.pb.v1.rs (L458-463)
```rust
pub struct ManageLedgerParameters {
    pub transfer_fee: Option<u64>,
    pub token_name: Option<String>,
    pub token_symbol: Option<String>,
    pub token_logo: Option<String>,
}
```

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

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L927-933)
```rust
        if let Some(transfer_fee) = args.transfer_fee {
            self.transfer_fee = Tokens::try_from(transfer_fee.clone()).unwrap_or_else(|e| {
                ic_cdk::trap(format!(
                    "failed to convert transfer fee {transfer_fee} to tokens: {e}"
                ))
            });
        }
```
