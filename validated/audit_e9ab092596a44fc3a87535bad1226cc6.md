### Title
Stale `minting_account` in ckERC20 Ledgers Deployed by the Ledger Suite Orchestrator — (File: `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`)

---

### Summary

The Ledger Suite Orchestrator (LSO) acts as a factory that deploys ICRC-1 ledger suites for ckERC20 tokens. At deployment time, it permanently bakes the current `minter_id` into each spawned ledger's `minting_account`. Because the ICRC-1 ledger's `minting_account` is immutable after initialization, if the minter principal ever changes (e.g., due to a security incident requiring minter replacement via NNS governance), all previously deployed ckERC20 ledgers retain the old minter as their sole authorized minting/burning principal. The new minter cannot mint or burn on those ledgers, breaking the entire ckERC20 deposit/withdrawal flow for all existing tokens.

---

### Finding Description

In `rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs`, the function `icrc1_ledger_init_arg()` constructs the initialization arguments for each newly deployed ckERC20 ICRC-1 ledger:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs:926-931
LedgerInitArgs {
    minting_account: LedgerAccount::from(minter_id),
    fee_collector_account: Some(LedgerAccount {
        owner: minter_id,
        subaccount: Some(LEDGER_FEE_SUBACCOUNT),
    }),
    ...
}
``` [1](#0-0) 

The `minter_id` is read from the orchestrator's own state at the moment of ledger deployment:

```rust
// rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs:553-558
let minter_id =
    state
        .minter_id()
        .cloned()
        .ok_or(InvalidAddErc20ArgError::InternalError(...))?;
``` [2](#0-1) 

The ICRC-1 ledger stores this value permanently in its own state:

```rust
// rs/ledger_suite/icrc1/ledger/src/lib.rs:708-709
minting_account,
fee_collector: fee_collector_account.map(FeeCollector::from),
``` [3](#0-2) 

The ICRC-1 ledger's `UpgradeArgs` provides no mechanism to change the `minting_account` after initialization. Inspecting the upgrade path:

```
type UpgradeArgs = record {
  change_fee_collector : opt ChangeFeeCollector;  // fee_collector CAN change
  // NO change_minting_account field exists
  ...
};
``` [4](#0-3) 

The `upgrade()` method in the ledger confirms: `minting_account` is never touched during upgrades. [5](#0-4) 

The orchestrator's `InitArg` accepts a `minter_id` that can be updated via NNS upgrade proposal, but this only affects **future** ledger deployments. All previously deployed ledgers are permanently bound to the old minter principal. [6](#0-5) 

---

### Impact Explanation

If the ckETH minter canister principal ever changes (e.g., the minter is redeployed to a new canister ID due to a critical security bug, or a governance decision to migrate to a new implementation), the following occurs:

1. The LSO's `minter_id` is updated via NNS upgrade proposal.
2. **New** ckERC20 ledgers deployed after the update correctly use the new minter as `minting_account`.
3. **All previously deployed** ckERC20 ledgers (e.g., ckUSDC, ckUSDT, ckLINK, etc.) retain the old minter principal as their `minting_account`.
4. The new minter cannot call `icrc1_transfer` from the minting account on those ledgers (it is not the minting account), so it cannot mint tokens when users deposit ERC-20 on Ethereum, and cannot burn tokens when users withdraw.
5. The entire ckERC20 deposit/withdrawal flow for all existing tokens is permanently broken with no on-chain recovery path, since `minting_account` is immutable.

The `fee_collector_account` is a lesser concern because the LSO can upgrade ledgers with `change_fee_collector` in `UpgradeArgs`, but the `minting_account` has no equivalent upgrade path.

**Impact: High** — all existing ckERC20 tokens become non-functional (no minting or burning possible).

---

### Likelihood Explanation

The trigger requires the ckETH minter canister to be replaced with a new canister ID. This is a rare but realistic governance event:

- A critical security vulnerability in the minter requiring emergency replacement.
- A planned migration to a new minter architecture.
- An NNS-controlled upgrade that changes the minter's canister ID.

Such events require an NNS governance proposal (not a malicious majority — a legitimate operational decision). The IC's chain-fusion infrastructure is designed to be upgradeable, and the minter has already been upgraded multiple times. The scenario is low-probability but not theoretical.

**Likelihood: Low-Medium**

---

### Recommendation

The LSO should not permanently bake the `minter_id` into each ledger's `minting_account` at deployment time without a recovery path. Two mitigations:

1. **Add `change_minting_account` to ICRC-1 ledger `UpgradeArgs`** (controlled by the ledger's controller, i.e., the LSO), so the LSO can update the `minting_account` on all managed ledgers when the minter principal changes.

2. **Alternatively**, when the LSO's `minter_id` is updated via upgrade, schedule an `UpgradeLedgerSuite` task for all managed ledgers that updates the `fee_collector_account` (already possible) and, once supported, the `minting_account`.

---

### Proof of Concept

1. LSO is initialized with `minter_id = sv3dd-oaaaa-aaaar-qacoa-cai` (the real ckETH minter).
2. NNS governance adds ckUSDC via `AddErc20Arg`. The LSO deploys a ledger with `minting_account = { owner: sv3dd-oaaaa-aaaar-qacoa-cai }`. [7](#0-6) 
3. A critical bug is found in the minter. NNS deploys a new minter at `new-minter-xxxx-cai` and upgrades the LSO with the new `minter_id`.
4. The ckUSDC ledger still has `minting_account = { owner: sv3dd-oaaaa-aaaar-qacoa-cai }`.
5. The new minter attempts to mint ckUSDC for a user deposit. The ledger rejects the mint because the caller is not the `minting_account`.
6. No NNS proposal can fix this — `minting_account` is immutable in the ICRC-1 ledger. [8](#0-7)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L553-558)
```rust
        let minter_id =
            state
                .minter_id()
                .cloned()
                .ok_or(InvalidAddErc20ArgError::InternalError(
                    "ERROR: minter principal not set in state".to_string(),
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L849-862)
```rust
    install_canister_once::<Ledger, _, _>(
        &args.contract,
        &args.ledger_compressed_wasm_hash,
        &LedgerArgument::Init(icrc1_ledger_init_arg(
            args.minter_id,
            args.ledger_init_arg.clone(),
            runtime.id().into(),
            more_controllers,
            cycles_for_archive_creation,
            index_principal,
        )),
        runtime,
    )
    .await?;
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L926-931)
```rust
    LedgerInitArgs {
        minting_account: LedgerAccount::from(minter_id),
        fee_collector_account: Some(LedgerAccount {
            owner: minter_id,
            subaccount: Some(LEDGER_FEE_SUBACCOUNT),
        }),
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L708-709)
```rust
            minting_account,
            fee_collector: fee_collector_account.map(FeeCollector::from),
```

**File:** rs/ledger_suite/icrc1/ledger/src/lib.rs (L913-976)
```rust
    pub fn upgrade(&mut self, sink: impl Sink + Clone, args: UpgradeArgs) {
        if let Some(upgrade_metadata_args) = args.metadata {
            // Only enforce strict validation if existing metadata has no invalid keys.
            // This allows ledgers with legacy invalid keys to still be upgraded.
            let existing_all_valid = self.metadata.iter().all(|(k, _)| k.is_valid());
            self.metadata =
                map_metadata_or_trap(upgrade_metadata_args, existing_all_valid, sink.clone());
        }
        if let Some(token_name) = args.token_name {
            self.token_name = token_name;
        }
        if let Some(token_symbol) = args.token_symbol {
            self.token_symbol = token_symbol;
        }
        if let Some(transfer_fee) = args.transfer_fee {
            self.transfer_fee = Tokens::try_from(transfer_fee.clone()).unwrap_or_else(|e| {
                ic_cdk::trap(format!(
                    "failed to convert transfer fee {transfer_fee} to tokens: {e}"
                ))
            });
        }
        if let Some(max_memo_length) = args.max_memo_length {
            if self.max_memo_length > max_memo_length {
                ic_cdk::trap(format!(
                    "The max len of the memo can be changed only to be bigger or equal than the current size. Current size: {}",
                    self.max_memo_length
                ));
            }
            self.max_memo_length = max_memo_length;
        }
        if let Some(change_fee_collector) = args.change_fee_collector {
            self.fee_collector = change_fee_collector.into();
            if self.fee_collector.as_ref().map(|fc| fc.fee_collector) == Some(self.minting_account)
            {
                ic_cdk::trap(
                    "The fee collector account cannot be the same account as the minting account",
                );
            }
        }
        if let Some(feature_flags) = args.feature_flags {
            if !feature_flags.icrc2 {
                log!(
                    sink,
                    "[ledger] feature flag icrc2 is deprecated and won't disable ICRC-2 anymore"
                );
            }
            self.feature_flags = feature_flags;
        }
        if let Some(change_archive_options) = args.change_archive_options {
            let mut maybe_archive = self.blockchain.archive.write().expect(
                "BUG: should be unreachable since upgrade has exclusive write access to the ledger",
            );
            if maybe_archive.is_none() {
                ic_cdk::trap(
                    "[ERROR]: Archive options cannot be changed, since there is no archive!",
                );
            }
            if let Some(archive) = maybe_archive.deref_mut() {
                change_archive_options.apply(archive);
            }
        }
        if let Some(index_principal) = args.index_principal {
            self.index_principal = Some(index_principal);
        }
```

**File:** rs/ledger_suite/icrc1/ledger/ledger.did (L140-150)
```text
type UpgradeArgs = record {
  metadata : opt vec record { text; MetadataValue };
  token_symbol : opt text;
  token_name : opt text;
  transfer_fee : opt nat;
  change_fee_collector : opt ChangeFeeCollector;
  max_memo_length : opt nat16;
  feature_flags : opt FeatureFlags;
  change_archive_options : opt ChangeArchiveOptions;
  index_principal : opt principal
};
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/candid/mod.rs (L14-19)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default, CandidType, Deserialize)]
pub struct InitArg {
    pub more_controller_ids: Vec<Principal>,
    pub minter_id: Option<Principal>,
    pub cycles_management: Option<CyclesManagement>,
}
```
