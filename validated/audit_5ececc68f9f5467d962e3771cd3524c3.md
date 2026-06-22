### Title
Missing `minter_id` Validation in Ledger Suite Orchestrator `InitArg` Allows Anonymous Minting Account on Spawned ckERC20 ICRC1 Ledgers - (File: `rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs`)

---

### Summary

The Ledger Suite Orchestrator's `State::try_from(InitArg)` does not validate that the `minter_id` field, when provided, is not `Principal::anonymous()`. Because `minter_id` is directly propagated as the `minting_account` of every ICRC1 ledger spawned for ckERC20 tokens, initializing the orchestrator with `minter_id = Some(Principal::anonymous())` would cause all subsequently spawned ckERC20 ledgers to accept the anonymous principal as their privileged minting authority — allowing any ingress sender to mint unlimited ckERC20 tokens.

---

### Finding Description

**Root cause — `validate_config()` only checks controller count:**

The `validate_config()` method called from `State::try_from(InitArg)` enforces only one constraint: that `more_controller_ids` does not exceed nine entries. It performs no validation on `minter_id`. [1](#0-0) 

The `TryFrom<InitArg>` implementation stores `minter_id` verbatim into state without any principal-validity check: [2](#0-1) 

**Propagation — `minter_id` becomes the ICRC1 ledger `minting_account`:**

When `AddErc20Arg` is processed, `validate_add_erc20` only checks that `minter_id` is `Some(...)`, not that it is a non-anonymous principal: [3](#0-2) 

That `minter_id` is then passed directly into `icrc1_ledger_init_arg`, which sets it as both the `minting_account` and the `fee_collector_account` owner of every spawned ICRC1 ledger: [4](#0-3) 

**Contrast with ckETH minter — which does validate:**

The ckETH minter's `State::validate_config()` explicitly rejects `Principal::anonymous()` for its `ledger_id`: [5](#0-4) 

No equivalent guard exists for `minter_id` in the orchestrator.

**`InitArg` struct for reference:** [6](#0-5) 

---

### Impact Explanation

**Vulnerability type:** Chain-fusion mint/burn bug — unauthorized minting of ckERC20 tokens.

If the orchestrator is initialized with `minter_id = Some(Principal::anonymous())`:

1. `State::try_from` succeeds — no validation rejects it.
2. When any `AddErc20Arg` upgrade proposal is later executed, `validate_add_erc20` succeeds because `minter_id` is `Some`.
3. `icrc1_ledger_init_arg` sets `minting_account = { owner: anonymous, subaccount: None }` on every spawned ckERC20 ICRC1 ledger.
4. In ICRC1, the minting account is the privileged account whose `icrc1_transfer` calls create tokens from thin air. Because the anonymous principal is reachable by any ingress sender without authentication, **any user on the IC can call `icrc1_transfer` as the anonymous principal and mint unlimited ckERC20 tokens** (e.g., ckUSDC, ckUSDT, ckPEPE) on those ledgers.
5. Additionally, the `NotifyErc20Added` task would attempt an inter-canister call to `Principal::anonymous()` as the minter, which would fail — silently breaking the minter notification flow.

The spawned ledgers are real ICRC1 canisters controlled by the orchestrator and NNS root; their token balances would be accepted by downstream DeFi integrations that trust the orchestrator's managed canister list.

---

### Likelihood Explanation

The orchestrator is deployed and upgraded exclusively via NNS governance proposals. A proposal supplying `minter_id = Some(Principal::anonymous())` must pass NNS voting. This is a significant barrier. However:

- Any neuron holder can submit a proposal; the missing on-chain validation means the canister itself provides no last-resort defense.
- The AeraVault report's exact concern applies: the deployer (here, the NNS proposer) could pass a correct-looking proposal that embeds a malicious `minter_id`, relying on reviewers missing the subtle principal value.
- Unlike the ckETH minter (which rejects `Principal::anonymous()` for `ledger_id`), the orchestrator has no analogous guard, creating an asymmetric trust assumption that is undocumented.

Likelihood is **low** in practice but the code provides zero protection against the misconfiguration.

---

### Recommendation

Add an explicit check in `State::validate_config()` (or directly in `TryFrom<InitArg> for State`) that rejects `minter_id = Some(Principal::anonymous())` and also rejects `Principal::anonymous()` appearing in `more_controller_ids`:

```rust
pub fn validate_config(&self) -> Result<(), InvalidStateError> {
    const MAX_ADDITIONAL_CONTROLLERS: usize = 9;
    if self.more_controller_ids.len() > MAX_ADDITIONAL_CONTROLLERS {
        return Err(InvalidStateError::TooManyAdditionalControllers { ... });
    }
    if let Some(minter_id) = &self.minter_id {
        if *minter_id == Principal::anonymous() {
            return Err(InvalidStateError::InvalidMinterId(
                "minter_id cannot be the anonymous principal".to_string(),
            ));
        }
    }
    for controller in &self.more_controller_ids {
        if *controller == Principal::anonymous() {
            return Err(InvalidStateError::InvalidControllerId(
                "more_controller_ids cannot contain the anonymous principal".to_string(),
            ));
        }
    }
    Ok(())
}
```

This mirrors the guard already present in the ckETH minter's `validate_config()` for `ledger_id`. [7](#0-6) 

---

### Proof of Concept

```
# 1. Install the orchestrator with minter_id = anonymous principal
didc encode -d ledger_suite_orchestrator.did -t '(OrchestratorArg)' \
  '(variant { InitArg = record {
      more_controller_ids = vec {};
      minter_id = opt principal "2vxsx-fae";   // anonymous principal
      cycles_management = null
  }})'

# 2. Register wasms via UpgradeArg (normal flow)

# 3. Submit AddErc20Arg for any ERC-20 token (e.g., USDC)
#    validate_add_erc20 succeeds: minter_id is Some(anonymous)

# 4. Orchestrator spawns ICRC1 ledger with:
#    minting_account = { owner = anonymous; subaccount = null }

# 5. Any user sends an ingress icrc1_transfer as the anonymous principal:
#    from = { owner = anonymous }  →  treated as minting account
#    to   = attacker_account
#    amount = 1_000_000_000_000   // unlimited ckERC20 minted
```

The `validate_add_erc20` path that accepts an anonymous `minter_id` without error: [8](#0-7) 

The `icrc1_ledger_init_arg` that propagates it as `minting_account`: [9](#0-8)

### Citations

**File:** rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs (L682-691)
```rust
    pub fn validate_config(&self) -> Result<(), InvalidStateError> {
        const MAX_ADDITIONAL_CONTROLLERS: usize = 9;
        if self.more_controller_ids.len() > MAX_ADDITIONAL_CONTROLLERS {
            return Err(InvalidStateError::TooManyAdditionalControllers {
                max: MAX_ADDITIONAL_CONTROLLERS,
                actual: self.more_controller_ids.len(),
            });
        }
        Ok(())
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/state/mod.rs (L768-788)
```rust
impl TryFrom<InitArg> for State {
    type Error = InvalidStateError;
    fn try_from(
        InitArg {
            more_controller_ids,
            minter_id,
            cycles_management,
        }: InitArg,
    ) -> Result<Self, Self::Error> {
        let state = Self {
            managed_canisters: Default::default(),
            completed_upgrades: Default::default(),
            cycles_management: cycles_management.unwrap_or_default(),
            more_controller_ids,
            minter_id,
            ledger_suite_version: Default::default(),
            active_tasks: Default::default(),
        };
        state.validate_config()?;
        Ok(state)
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L544-596)
```rust
impl InstallLedgerSuiteArgs {
    pub fn validate_add_erc20(
        state: &State,
        wasm_store: &WasmStore,
        args: AddErc20Arg,
    ) -> Result<InstallLedgerSuiteArgs, InvalidAddErc20ArgError> {
        let contract = Erc20Token::try_from(args.contract.clone())
            .map_err(|e| InvalidAddErc20ArgError::InvalidErc20Contract(e.to_string()))?;
        let token_id = TokenId::from(contract.clone());
        let minter_id =
            state
                .minter_id()
                .cloned()
                .ok_or(InvalidAddErc20ArgError::InternalError(
                    "ERROR: minter principal not set in state".to_string(),
                ))?;
        if let Some(_canisters) = state.managed_canisters(&token_id) {
            return Err(InvalidAddErc20ArgError::Erc20ContractAlreadyManaged(
                contract,
            ));
        }
        let (ledger_compressed_wasm_hash, index_compressed_wasm_hash) = {
            let LedgerSuiteVersion {
                ledger_compressed_wasm_hash,
                index_compressed_wasm_hash,
                archive_compressed_wasm_hash: _,
            } = state
                .ledger_suite_version()
                .expect("ERROR: ledger suite version missing");
            //TODO XC-138: move read method to state and ensure that hash is in store and remove this.
            assert!(
                //nothing can be changed in AddErc20Arg to fix this.
                wasm_store_contain::<Ledger>(wasm_store, ledger_compressed_wasm_hash),
                "BUG: ledger compressed wasm hash missing"
            );
            assert!(
                //nothing can be changed in AddErc20Arg to fix this.
                wasm_store_contain::<Index>(wasm_store, index_compressed_wasm_hash),
                "BUG: index compressed wasm hash missing"
            );
            (
                ledger_compressed_wasm_hash.clone(),
                index_compressed_wasm_hash.clone(),
            )
        };
        Ok(Self {
            contract,
            minter_id,
            ledger_init_arg: args.ledger_init_arg,
            ledger_compressed_wasm_hash,
            index_compressed_wasm_hash,
        })
    }
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/scheduler/mod.rs (L903-950)
```rust
fn icrc1_ledger_init_arg(
    minter_id: Principal,
    ledger_init_arg: LedgerInitArg,
    archive_controller_id: PrincipalId,
    archive_more_controller_ids: Vec<PrincipalId>,
    cycles_for_archive_creation: Nat,
    index_principal: Principal,
) -> LedgerInitArgs {
    use ic_icrc1_ledger::FeatureFlags as LedgerFeatureFlags;
    use icrc_ledger_types::icrc::generic_metadata_value::MetadataValue as LedgerMetadataValue;
    use icrc_ledger_types::icrc::metadata_key::MetadataKey;
    use icrc_ledger_types::icrc1::account::Account as LedgerAccount;

    const LEDGER_FEE_SUBACCOUNT: [u8; 32] = [
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0x0f, 0xee,
    ];
    const MAX_MEMO_LENGTH: u16 = 80;
    const ICRC2_FEATURE: LedgerFeatureFlags = LedgerFeatureFlags {
        icrc2: true,
        icrc152: false,
    };

    LedgerInitArgs {
        minting_account: LedgerAccount::from(minter_id),
        fee_collector_account: Some(LedgerAccount {
            owner: minter_id,
            subaccount: Some(LEDGER_FEE_SUBACCOUNT),
        }),
        initial_balances: vec![],
        transfer_fee: ledger_init_arg.transfer_fee,
        decimals: Some(ledger_init_arg.decimals),
        token_name: ledger_init_arg.token_name,
        token_symbol: ledger_init_arg.token_symbol,
        metadata: vec![(
            MetadataKey::ICRC1_LOGO.to_string(),
            LedgerMetadataValue::from(ledger_init_arg.token_logo),
        )],
        archive_options: icrc1_archive_options(
            archive_controller_id,
            archive_more_controller_ids,
            cycles_for_archive_creation,
        ),
        max_memo_length: Some(MAX_MEMO_LENGTH),
        feature_flags: Some(ICRC2_FEATURE),
        index_principal: Some(index_principal),
    }
}
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L145-155)
```rust
    pub fn validate_config(&self) -> Result<(), InvalidStateError> {
        if self.ecdsa_key_name.trim().is_empty() {
            return Err(InvalidStateError::InvalidEcdsaKeyName(
                "ecdsa_key_name cannot be blank".to_string(),
            ));
        }
        if self.cketh_ledger_id == Principal::anonymous() {
            return Err(InvalidStateError::InvalidLedgerId(
                "ledger_id cannot be the anonymous principal".to_string(),
            ));
        }
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
