### Title
SNS Token Symbol/Name Squatting via Missing Global Uniqueness Enforcement - (`rs/nervous_system/common/src/ledger_validation.rs`)

---

### Summary

The Internet Computer's SNS deployment pipeline validates token symbols and names only for format (length, whitespace, a two-entry banned list), but performs **no global uniqueness check** against already-deployed SNS instances. Any NNS neuron holder can submit a `CreateServiceNervousSystem` proposal registering a token symbol identical to an existing popular SNS token (e.g., "OC", "CHAT", "SNS1"). If the proposal passes, a second SNS with a duplicate symbol is permanently deployed on-chain, with no protocol-level mechanism to prevent or detect the collision.

---

### Finding Description

`validate_token_symbol` in `rs/nervous_system/common/src/ledger_validation.rs` enforces only three constraints: length (3–10 chars), no leading/trailing whitespace, and membership in a two-entry banned list (`["ICP", "DFINITY"]`): [1](#0-0) 

All other symbols — including those already in use by live SNS instances — pass validation. This function is called by `SnsInitPayload::validate_token_symbol` and `SnsInitPayload::validate_token_name`: [2](#0-1) 

Both `validate_pre_execution` and `validate_post_execution` invoke these checks: [3](#0-2) 

The SNS-W canister's `do_deploy_new_sns` calls `get_and_validate_sns_init_payload()` which runs `validate_post_execution()`, but never queries the list of already-deployed SNS instances for symbol/name collisions: [4](#0-3) 

The list of deployed SNS instances is stored in `deployed_sns_list` and is queryable via `list_deployed_snses`, but is never consulted during deployment validation: [5](#0-4) 

A second attack surface exists via `ManageLedgerParameters` SNS governance proposals, which allow an existing SNS to rename its token symbol post-deployment. The validation in `validate_and_render_manage_ledger_parameters` also only calls `ledger_validation::validate_token_symbol`, with no cross-SNS uniqueness check: [6](#0-5) 

---

### Impact Explanation

Two or more SNS instances can simultaneously hold the same `token_symbol` (e.g., `"OC"`) and `token_name`. This causes:

1. **User confusion and fraud**: Wallets, DEXes, and explorers that display tokens by symbol will show multiple indistinguishable entries. Users can be deceived into purchasing the wrong token.
2. **Brand squatting**: A malicious actor can register a symbol identical to a well-known SNS project, then conduct a swap to raise ICP from users who believe they are investing in the original project.
3. **No on-chain remedy**: Once deployed, an SNS's token symbol is permanent unless the SNS community itself votes to change it. There is no NNS-level mechanism to forcibly rename or deregister a squatted symbol.

---

### Likelihood Explanation

The attack requires submitting and passing an NNS `CreateServiceNervousSystem` proposal. NNS neuron holders are explicitly listed as in-scope governance users. The NNS voting interface provides no automated duplicate-symbol detection; voters must manually cross-reference all existing SNS deployments. Given the growing number of SNS instances and the volume of NNS proposals, a squatting proposal can plausibly pass unnoticed, especially if the symbol is slightly obscure or the proposal is submitted during a high-volume period. The `ManageLedgerParameters` path requires only SNS-level governance approval, which is a lower bar than NNS approval.

---

### Recommendation

1. **Enforce global symbol uniqueness at deployment time**: In `do_deploy_new_sns` (or in `get_and_validate_sns_init_payload`), query `deployed_sns_list`, fetch each deployed SNS ledger's `icrc1_symbol` and `icrc1_name`, and reject the deployment if a collision is found.
2. **Enforce uniqueness on `ManageLedgerParameters`**: In `validate_and_render_manage_ledger_parameters`, similarly check the proposed new symbol against all deployed SNS instances.
3. **Expand the banned list**: At minimum, add all currently deployed SNS token symbols to `BANNED_TOKEN_SYMBOLS` and `BANNED_TOKEN_NAMES` in `rs/nervous_system/common/src/ledger_validation.rs`, and update this list as new SNS instances are deployed.

---

### Proof of Concept

**Attacker-controlled entry path**:

1. Attacker (NNS neuron holder) observes that the popular SNS "OpenChat" uses token symbol `"OC"`.
2. Attacker submits a `CreateServiceNervousSystem` NNS proposal with `token_symbol = "OC"` and `token_name = "OpenChat"`.
3. `SnsInitPayload::validate_pre_execution()` is called during proposal submission. It calls `validate_token_symbol("OC")`:
   - Length check: 2 chars — **wait**, minimum is 3. Let's use `"OCH"` instead, or the attacker uses `"OC2"` (3 chars). Or they use the exact same symbol if it's ≥ 3 chars, e.g., `"CHAT"`.
4. `validate_token_symbol("CHAT")` passes: length 4 ✓, no whitespace ✓, not in `["ICP", "DFINITY"]` ✓.
5. The proposal passes NNS governance vote (voters do not notice the duplicate).
6. `execute_create_service_nervous_system_proposal` calls `call_deploy_new_sns`, which calls `do_deploy_new_sns`.
7. `do_deploy_new_sns` calls `get_and_validate_sns_init_payload()` → `validate_post_execution()` → `validate_token_symbol("CHAT")` → passes again.
8. A new SNS is deployed with `token_symbol = "CHAT"`, identical to the existing SNS.
9. Both SNS instances now appear in `list_deployed_snses` with the same symbol. Wallets and DEXes display both as `"CHAT"`. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/nervous_system/common/src/ledger_validation.rs (L17-48)
```rust
/// Token Symbols that can not be used.
const BANNED_TOKEN_SYMBOLS: &[&str] = &["ICP", "DFINITY"];

/// Token Names that can not be used.
const BANNED_TOKEN_NAMES: &[&str] = &["internetcomputer", "internetcomputerprotocol"];

pub fn validate_token_symbol(token_symbol: &str) -> Result<(), String> {
    if token_symbol.len() > MAX_TOKEN_SYMBOL_LENGTH {
        return Err(format!(
            "Error: token-symbol must be fewer than {} characters, given character count: {}",
            MAX_TOKEN_SYMBOL_LENGTH,
            token_symbol.len()
        ));
    }

    if token_symbol.len() < MIN_TOKEN_SYMBOL_LENGTH {
        return Err(format!(
            "Error: token-symbol must be greater than {} characters, given character count: {}",
            MIN_TOKEN_SYMBOL_LENGTH,
            token_symbol.len()
        ));
    }

    if token_symbol != token_symbol.trim() {
        return Err("Token symbol must not have leading or trailing whitespaces".to_string());
    }

    if BANNED_TOKEN_SYMBOLS.contains(&token_symbol.to_uppercase().as_ref()) {
        return Err("Banned token symbol, please chose another one.".to_string());
    }

    Ok(())
```

**File:** rs/sns/init/src/lib.rs (L848-890)
```rust
    pub fn validate_pre_execution(&self) -> Result<Self, String> {
        let validation_fns = [
            self.validate_token_symbol(),
            self.validate_token_name(),
            self.validate_token_logo(),
            self.validate_token_distribution(),
            self.validate_participation_constraints(),
            self.validate_neuron_minimum_stake_e8s(),
            self.validate_neuron_minimum_dissolve_delay_to_vote_seconds(),
            self.validate_neuron_basket_construction_params(),
            self.validate_proposal_reject_cost_e8s(),
            self.validate_transaction_fee_e8s(),
            self.validate_fallback_controller_principal_ids(),
            self.validate_url(),
            self.validate_logo(),
            self.validate_description(),
            self.validate_name(),
            self.validate_initial_reward_rate_basis_points(),
            self.validate_final_reward_rate_basis_points(),
            self.validate_reward_rate_transition_duration_seconds(),
            self.validate_max_dissolve_delay_seconds(),
            self.validate_max_neuron_age_seconds_for_age_bonus(),
            self.validate_max_dissolve_delay_bonus_percentage(),
            self.validate_max_age_bonus_percentage(),
            self.validate_initial_voting_period_seconds(),
            self.validate_wait_for_quiet_deadline_increase_seconds(),
            self.validate_dapp_canisters(),
            self.validate_confirmation_text(),
            self.validate_restricted_countries(),
            // Ensure that the values that can only be known after the execution
            // of the CreateServiceNervousSystem proposal are not set.
            self.validate_nns_proposal_id_pre_execution(),
            self.validate_swap_start_timestamp_seconds_pre_execution(),
            self.validate_swap_due_timestamp_seconds_pre_execution(),
            self.validate_neurons_fund_participation_constraints(true),
            self.validate_neurons_fund_participation(),
            // Obsolete fields are not set
            self.validate_min_icp_e8s(),
            self.validate_max_icp_e8s(),
        ];

        self.join_validation_results(&validation_fns)
    }
```

**File:** rs/sns/init/src/lib.rs (L961-977)
```rust
    fn validate_token_symbol(&self) -> Result<(), String> {
        let token_symbol = self
            .token_symbol
            .as_ref()
            .ok_or_else(|| "Error: token-symbol must be specified".to_string())?;

        ledger_validation::validate_token_symbol(token_symbol)
    }

    fn validate_token_name(&self) -> Result<(), String> {
        let token_name = self
            .token_name
            .as_ref()
            .ok_or_else(|| "Error: token-name must be specified".to_string())?;

        ledger_validation::validate_token_name(token_name)
    }
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L681-689)
```rust
    /// Returns a list of Deployed SNS root CanisterId's and the subnet they were deployed to.
    pub fn list_deployed_snses(
        &self,
        _list_sns_payload: ListDeployedSnsesRequest,
    ) -> ListDeployedSnsesResponse {
        ListDeployedSnsesResponse {
            instances: self.deployed_sns_list.clone(),
        }
    }
```

**File:** rs/nns/sns-wasm/src/sns_wasm.rs (L808-835)
```rust
    async fn do_deploy_new_sns(
        thread_safe_sns: &'static LocalKey<RefCell<SnsWasmCanister<M>>>,
        canister_api: &impl CanisterApi,
        nns_root_canister_client: &impl NnsRootCanisterClient,
        deploy_new_sns_request: DeployNewSnsRequest,
    ) -> Result<(SubnetId, SnsCanisterIds, Vec<Canister>), DeployError> {
        let sns_init_payload = deploy_new_sns_request
            .get_and_validate_sns_init_payload()
            .map_err(validation_deploy_error)?;

        let dapp_canisters = &sns_init_payload
            .dapp_canisters
            .as_ref()
            .map(|dapp_canisters| dapp_canisters.canisters.as_slice())
            .unwrap_or_default();

        let subnet_id = thread_safe_sns
            .with(|sns_canister| sns_canister.borrow().get_available_sns_subnet())
            .map_err(validation_deploy_error)?;

        // Ensure we have WASMs available to install before proceeding (avoid unnecessary cleanup)
        let latest_wasms = thread_safe_sns
            .with(|sns_wasms| sns_wasms.borrow().get_latest_version_wasms())
            .map_err(validation_deploy_error)?;

        canister_api
            .this_canister_has_enough_cycles(SNS_CREATION_FEE)
            .map_err(validation_deploy_error)?;
```

**File:** rs/sns/governance/src/proposal.rs (L1782-1784)
```rust
    if let Some(token_symbol) = token_symbol {
        ledger_validation::validate_token_symbol(token_symbol)?;
        render += &format!("# Set token symbol: {token_symbol}. \n",);
```

**File:** rs/nns/governance/src/governance.rs (L4487-4525)
```rust
    async fn execute_create_service_nervous_system_proposal(
        &mut self,
        create_service_nervous_system: CreateServiceNervousSystem,
        neurons_fund_participation_constraints: Option<NeuronsFundParticipationConstraints>,
        current_timestamp_seconds: u64,
        proposal_id: ProposalId,
        random_swap_start_time: GlobalTimeOfDay,
        initial_neurons_fund_participation_snapshot: NeuronsFundSnapshot,
    ) -> Result<(), GovernanceError> {
        let is_start_time_unspecified = create_service_nervous_system
            .swap_parameters
            .as_ref()
            .map(|swap_parameters| swap_parameters.start_time.is_none())
            .unwrap_or(false);

        // Step 1.1: Convert proposal into SnsInitPayload.
        let sns_init_payload = Self::make_sns_init_payload(
            create_service_nervous_system,
            neurons_fund_participation_constraints,
            current_timestamp_seconds,
            proposal_id,
            random_swap_start_time,
        )
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!(
                    "Failed to convert CreateServiceNervousSystem proposal to SnsInitPayload: {err}",
                ),
            )
        })?;

        // Step 1.2: Validate the SnsInitPayload.
        sns_init_payload.validate_post_execution().map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!("Failed to validate SnsInitPayload: {err}"),
            )
        })?;
```
