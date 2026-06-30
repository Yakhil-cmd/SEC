### Title
Unprotected `new()` Initialization Allows Frontrunning to Seize Full Engine Ownership - (File: `engine/src/contract_methods/admin.rs`)

### Summary
The Aurora Engine's `new()` initialization function performs no caller check. Any NEAR account can call it before the legitimate deployer, setting themselves as `owner_id` and `key_manager`, gaining complete administrative control over the engine and all bridged funds.

### Finding Description
The `new()` function in `engine/src/contract_methods/admin.rs` initializes the engine's `EngineState`, which includes `owner_id` (the account authorized to upgrade the contract), `chain_id`, `upgrade_delay_blocks`, and `key_manager`. The only guard is a check that the state has not already been written: [1](#0-0) 

There is no check on `env.predecessor_account_id()` — no restriction on who may call this function. It is exposed as a public NEAR contract method with no access control: [2](#0-1) 

The `EngineState` written by `new()` sets the `owner_id` and `key_manager` fields that gate every privileged operation in the engine: [3](#0-2) 

On NEAR, contract deployment and initialization are separate transactions. The workspace deployment helper shows this two-step pattern explicitly — `deploy()` is awaited, then `new()` is called as a separate transaction: [4](#0-3) 

Between these two transactions, any NEAR account can call `new()` with attacker-controlled arguments, setting themselves as `owner_id` and `key_manager`. The deployer's subsequent `new()` call will fail with `ERR_ALREADY_INITIALIZED`, but the damage is already done.

### Impact Explanation
Once an attacker is set as `owner_id`, they control every privileged function gated by `require_owner_only`: [5](#0-4) 

This includes:
- `upgrade()` — deploy arbitrary contract code to the engine account, enabling theft of all bridged ETH and tokens
- `attach_full_access_key()` — add a full access key to the engine account, granting complete NEAR account control
- `pause_contract()` — permanently freeze all user funds
- `set_eth_connector_contract_account()` — redirect the bridge connector to a malicious contract
- `stage_upgrade()` / `deploy_upgrade()` — replace the engine with malicious code [6](#0-5) 

**Impact**: Critical. Direct theft of all bridged user funds and/or permanent freezing of funds.

### Likelihood Explanation
NEAR does not have a traditional mempool, but deployment and initialization are separate on-chain transactions. Any NEAR account that observes the deployment transaction (visible once included in a block) can submit a `new()` call targeting the freshly deployed contract before the deployer's initialization transaction is processed. The attacker only needs to submit their `new()` call in the same or next block after deployment. This is a realistic, low-skill attack requiring only NEAR RPC access.

### Recommendation
Add a caller restriction to `new()`. The simplest correct fix is to assert that `env.predecessor_account_id() == env.current_account_id()`, ensuring only the contract account itself (via a batch transaction) can initialize it. Alternatively, require that deployment and initialization are always performed atomically in a single NEAR batch action (CreateAccount + DeployContract + FunctionCall("new")), and document this as a hard requirement. The XCC router already demonstrates the correct pattern — it uses a single batch for deploy + initialize: [7](#0-6) 

### Proof of Concept
1. Attacker monitors NEAR for a new Aurora Engine contract deployment (e.g., via RPC polling or indexer).
2. Upon detecting the deployment transaction (before the deployer's `new()` call is processed), attacker submits:
   ```
   new({ chain_id: <any>, owner_id: "attacker.near", upgrade_delay_blocks: 0, key_manager: "attacker.near" })
   ```
   targeting the newly deployed engine account.
3. Attacker's `new()` is processed first; `EngineState` is written with `owner_id = "attacker.near"`.
4. Deployer's `new()` call fails with `ERR_ALREADY_INITIALIZED`.
5. Attacker calls `attach_full_access_key()` or `upgrade()` with malicious contract bytecode.
6. All bridged ETH and ERC-20 tokens are drained. [8](#0-7)

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

**File:** engine/src/contract_methods/admin.rs (L483-512)
```rust
pub fn attach_full_access_key<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<(), ContractError> {
    let state = state::get_state(&io)?;

    require_running(&state)?;
    require_owner_only(&state, &env.predecessor_account_id())?;

    let public_key = serde_json::from_slice::<FullAccessKeyArgs>(&io.read_input().to_vec())
        .map(|args| args.public_key)
        .map_err(|_| errors::ERR_JSON_DESERIALIZE)?;
    let current_account_id = env.current_account_id();
    let action = PromiseAction::AddFullAccessKey {
        public_key,
        nonce: 0, // not actually used - depends on block height
    };
    let promise = PromiseBatchAction {
        target_account_id: current_account_id,
        actions: vec![action],
    };
    // SAFETY: This action is dangerous because it adds a new full access key (FAK) to the Engine account.
    // However, it is safe to do so here because of the `require_owner_only` check above; only the
    // (trusted) owner account can add a new FAK.
    let promise_id = handler.promise_create_batch(&promise);

    handler.promise_return(promise_id);

    Ok(())
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

**File:** engine-workspace/src/lib.rs (L107-126)
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

**File:** engine/src/xcc.rs (L120-130)
```rust
            promise_actions.push(PromiseAction::CreateAccount);
            promise_actions.push(PromiseAction::Transfer {
                amount: fund_amount,
            });
            promise_actions.push(PromiseAction::DeployContract { code });
            promise_actions.push(PromiseAction::FunctionCall {
                name: "initialize".into(),
                args: init_args.into_bytes(),
                attached_yocto: ZERO_YOCTO,
                gas: INITIALIZE_GAS,
            });
```
