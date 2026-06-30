### Title
Unprotected `new()` Initialization Function Allows Ownership Hijacking - (File: `engine/src/contract_methods/admin.rs`)

---

### Summary

The Aurora Engine's `new()` initialization function performs no caller authentication. Any NEAR account can call it before the legitimate deployer does, setting an arbitrary `owner_id`, `chain_id`, and `key_manager`. Because the owner controls contract upgrades, a successful front-run yields full, irrecoverable control of the Aurora Engine — enabling arbitrary code deployment and theft of all bridged funds.

---

### Finding Description

The `new()` function in `engine/src/contract_methods/admin.rs` is the sole initialization entry point for the Aurora Engine. It is exposed as a public `#[no_mangle]` NEAR contract method via `engine/src/lib.rs`. [1](#0-0) 

The only guard present is a re-initialization check:

```rust
if state::get_state(&io).is_ok() {
    return Err(b"ERR_ALREADY_INITIALIZED".into());
}
```

There is **no check on `env.predecessor_account_id()`**. Any NEAR account that calls `new()` before the deployer does will have its supplied `NewCallArgs` accepted as the canonical engine state. [2](#0-1) 

The `NewCallArgs` struct (all versions V1–V4) includes `owner_id`, `chain_id`, `upgrade_delay_blocks`, and `key_manager` — all of which the attacker controls: [3](#0-2) 

The deployment helper `deploy_and_init` in `engine-workspace/src/lib.rs` issues the `DeployContract` action and the `new()` call as **two separate transactions** (two separate `.await` points), creating an observable front-running window: [4](#0-3) 

Once the attacker's `owner_id` is stored in `EngineState`, all owner-gated functions enforce it via `require_owner_only`: [5](#0-4) 

The `upgrade()` function, gated exclusively by `require_owner_only`, allows the owner to deploy arbitrary new WASM bytecode to the Aurora Engine account: [6](#0-5) 

The legitimate deployer's subsequent `new()` call will fail with `ERR_ALREADY_INITIALIZED`, and they have no recovery path because `set_owner` is also owner-gated. [7](#0-6) 

---

### Impact Explanation

**Critical — Direct theft of all user funds and permanent fund freeze.**

After front-running `new()`, the attacker:

1. Calls `upgrade()` as the fraudulent owner to deploy malicious WASM that redirects all ETH/ERC-20 bridge withdrawals to the attacker's address.
2. Alternatively calls `pause_contract()` to permanently freeze the engine, blocking all user withdrawals.
3. Can call `stage_upgrade()` + `upgrade()` to replace the entire contract logic.

All bridged ETH and ERC-20 tokens held by the Aurora Engine are at risk. The legitimate team has no recourse because every privileged recovery function (`set_owner`, `resume_contract`, `upgrade`) requires the attacker-controlled `owner_id`.

---

### Likelihood Explanation

**High.** NEAR transactions are publicly visible before finalization. A bot monitoring the NEAR mempool for `DeployContract` actions targeting the Aurora account ID can immediately submit a `new()` call in the same or next block. The `deploy_and_init` pattern in the workspace confirms the two-transaction gap exists in the standard deployment flow. No special privileges or leaked keys are required — only the ability to submit a NEAR transaction.

---

### Recommendation

Add a caller check inside `new()` before writing any state. The contract account itself is the only valid caller during a batch deploy+init:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    // Only the contract account itself may initialize (enforced by batch atomicity)
    if env.predecessor_account_id() != env.current_account_id() {
        return Err(b"ERR_NOT_ALLOWED".into());
    }
    // ... rest of initialization
}
```

Additionally, the deployment tooling should use a single atomic NEAR batch transaction containing both `DeployContract` and `FunctionCall(new)` actions, eliminating the front-running window entirely.

---

### Proof of Concept

1. Deployer broadcasts `DeployContract` for `aurora` account — this is visible in the NEAR mempool.
2. Attacker observes the pending deploy and immediately submits:
   ```
   FunctionCall {
     contract_id: "aurora",
     method_name: "new",
     args: NewCallArgsV2 {
       chain_id: <legitimate chain id>,
       owner_id: "attacker.near",
       upgrade_delay_blocks: 0,
     }
   }
   ```
3. Attacker's `new()` call lands first (or in the same block before the deployer's init tx). State is written with `owner_id = "attacker.near"`.
4. Deployer's `new()` call fails: `ERR_ALREADY_INITIALIZED`.
5. Attacker calls `upgrade()` from `attacker.near`, supplying malicious WASM that drains the bridge on every `withdraw` call.
6. All user funds are stolen; the legitimate team cannot recover ownership because `set_owner` requires `predecessor == state.owner_id == "attacker.near"`. [1](#0-0) [6](#0-5) [8](#0-7)

### Citations

**File:** engine/src/contract_methods/admin.rs (L55-88)
```rust
#[named]
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }

    let input = io.read_input().to_vec();
    let args = NewCallArgs::deserialize(&input).map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;

    let initial_hashchain = args.initial_hashchain();
    let state: EngineState = args.into();

    if let Some(block_hashchain) = initial_hashchain {
        let block_height = env.block_height();
        let mut hashchain = Hashchain::new(
            state.chain_id,
            env.current_account_id(),
            block_height,
            block_hashchain,
        );

        hashchain.add_block_tx(
            block_height,
            function_name!(),
            &input,
            &[],
            &Bloom::default(),
        )?;
        crate::hashchain::save_hashchain(&mut io, &hashchain)?;
    }

    state::set_state(&mut io, &state)?;
    Ok(())
}
```

**File:** engine/src/contract_methods/admin.rs (L103-120)
```rust
#[named]
pub fn set_owner<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;

        require_running(&state)?;
        require_owner_only(&state, &env.predecessor_account_id())?;

        let args: SetOwnerArgs = io.read_input_borsh()?;
        if state.owner_id == args.new_owner {
            return Err(errors::ERR_SAME_OWNER.into());
        }

        state.owner_id = args.new_owner;
        state::set_state(&mut io, &state)?;

        Ok(())
    })
```

**File:** engine/src/contract_methods/admin.rs (L169-206)
```rust
pub fn upgrade<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<(), ContractError> {
    let state = state::get_state(&io)?;
    require_running(&state)?;
    require_owner_only(&state, &env.predecessor_account_id())?;

    let input = io.read_input().to_vec();
    let (code, state_migration_gas) = match UpgradeParams::try_from_slice(&input) {
        Ok(args) => (
            args.code,
            args.state_migration_gas
                .map_or(GAS_FOR_STATE_MIGRATION, NearGas::new),
        ),
        Err(_) => (input, GAS_FOR_STATE_MIGRATION), // Backward compatibility
    };

    let target_account_id = env.current_account_id();
    let batch = PromiseBatchAction {
        target_account_id,
        actions: vec![
            PromiseAction::DeployContract { code },
            PromiseAction::FunctionCall {
                name: "state_migration".to_string(),
                args: vec![],
                attached_yocto: ZERO_YOCTO,
                gas: state_migration_gas,
            },
        ],
    };
    let promise_id = handler.promise_create_batch(&batch);

    handler.promise_return(promise_id);

    Ok(())
}
```

**File:** engine-types/src/parameters/engine.rs (L100-115)
```rust
#[derive(Debug, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize, Serialize, Deserialize)]
pub struct NewCallArgsV4 {
    /// Chain id, according to the EIP-115 / ethereum-lists spec.
    #[serde(with = "chain_id_deserialize")]
    pub chain_id: RawU256,
    /// Account which can upgrade this contract.
    /// Use empty to disable updatability.
    pub owner_id: AccountId,
    /// How many blocks after staging upgrade can deploy it.
    pub upgrade_delay_blocks: u64,
    /// Relayer keys manager.
    pub key_manager: AccountId,
    /// Initial value of the hashchain.
    /// If none is provided then the hashchain will start disabled.
    pub initial_hashchain: Option<RawH256>,
}
```

**File:** engine-workspace/src/lib.rs (L107-125)
```rust
        let contract = account
            .deploy(
                &self
                    .code
                    .ok_or_else(|| anyhow::anyhow!("WASM wasn't set"))?,
            )
            .await?;
        let engine = EngineContract {
            account,
            contract,
            public_key,
            node,
        };

        engine
            .new(self.chain_id, self.owner_id, self.upgrade_delay_blocks)
            .transact()
            .await
            .map_err(|e| anyhow::anyhow!("Error while initialize aurora contract: {e}"))?;
```

**File:** engine/src/contract_methods/mod.rs (L79-87)
```rust
pub fn require_owner_only(
    state: &state::EngineState,
    predecessor_account_id: &AccountId,
) -> Result<(), ContractError> {
    if &state.owner_id != predecessor_account_id {
        return Err(errors::ERR_NOT_ALLOWED.into());
    }
    Ok(())
}
```

**File:** engine/src/lib.rs (L76-83)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn new() {
        let io = Runtime;
        let env = Runtime;
        contract_methods::admin::new(io, &env)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```
