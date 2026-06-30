### Title
Unguarded `new()` Initialization Can Be Front-Run to Hijack Engine Ownership — (`engine/src/contract_methods/admin.rs`)

---

### Summary

The Aurora Engine's `new()` initialization function has no caller access control. It only checks whether state already exists. If the WASM deployment and the `new()` call are submitted as separate NEAR transactions (not in a single atomic batch), any external account can call `new()` in the window between deployment and legitimate initialization, setting themselves as `owner_id` and gaining full administrative control of the engine.

---

### Finding Description

The `new()` function in `engine/src/contract_methods/admin.rs` is the sole initialization entry point for the Aurora Engine contract. It accepts arbitrary `NewCallArgs` (including `owner_id`, `chain_id`, `upgrade_delay_blocks`) and writes them as the canonical engine state:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    let input = io.read_input().to_vec();
    let args = NewCallArgs::deserialize(&input)...;
    let state: EngineState = args.into();
    state::set_state(&mut io, &state)?;
    Ok(())
}
``` [1](#0-0) 

The only guard is `state::get_state(&io).is_ok()` — a one-time idempotency check. There is no `require_owner_only`, no `predecessor_account_id` check, and no restriction on who may call this function. The NEAR contract entrypoint exposes it unconditionally:

```rust
pub extern "C" fn new() {
    let io = Runtime;
    let env = Runtime;
    contract_methods::admin::new(io, &env)...sdk_unwrap();
}
``` [2](#0-1) 

The workspace deployment helper shows that `deploy` and `new` are issued as **separate** awaited transactions, not as a single atomic NEAR batch:

```rust
let contract = account.deploy(&self.code...).await?;
// ...
engine.new(self.chain_id, self.owner_id, self.upgrade_delay_blocks)
    .transact().await...;
``` [3](#0-2) 

This creates a race window between the `DeployContract` receipt and the `new()` receipt.

---

### Impact Explanation

An attacker who calls `new()` first supplies their own `owner_id`. The `owner_id` field controls all privileged operations: `set_owner`, `stage_upgrade`, `upgrade`, `pause_precompiles`, `set_eth_connector_contract_account`, and more. [4](#0-3) 

With ownership, the attacker can call `stage_upgrade` to stage arbitrary WASM code, then call `deploy_upgrade` (which is callable by anyone per the changelog) to replace the engine with malicious code. This enables direct theft of all bridged ETH and ERC-20 tokens held by the engine, or permanent freezing of all user funds.

**Impact: Critical** — direct theft of user funds at rest, or permanent fund freeze.

---

### Likelihood Explanation

NEAR Protocol blocks are produced approximately every 1 second. If the deployer submits the `DeployContract` transaction and the `new()` transaction in separate blocks (or even separate transactions within the same block), an attacker monitoring the chain can observe the deployment receipt and submit their own `new()` call before the legitimate one is processed. NEAR has no gas-price-based ordering, but validators process transactions from the mempool and an attacker can submit their transaction immediately upon seeing the deployment. The workspace code confirms the two-step pattern is the default deployment path.

**Likelihood: Medium** — requires non-atomic deployment (the default pattern shown in the codebase) and active monitoring of the NEAR chain.

---

### Recommendation

Either:

1. **Atomic deployment**: Always deploy the engine WASM and call `new()` in a single NEAR batch transaction (`DeployContract` + `FunctionCall("new")` as batch actions on the same account). This is the same mitigation applied to the XCC Router, which explicitly documents this requirement. [5](#0-4) 

2. **Access control on `new()`**: Add a check that `env.predecessor_account_id() == env.current_account_id()`, restricting initialization to self-calls only (i.e., only callable from a batch action on the engine account itself).

---

### Proof of Concept

1. Deployer submits transaction T1: `DeployContract` (uploads Aurora Engine WASM to `aurora.near`).
2. T1 is included in block N. The contract is now deployed but `state::get_state` returns `Err` (not initialized).
3. Attacker observes block N, constructs `NewCallArgs { owner_id: "attacker.near", chain_id: ..., upgrade_delay_blocks: 0 }`.
4. Attacker submits transaction T2: calls `new()` on `aurora.near` with attacker-controlled args. T2 is included in block N or N+1.
5. Deployer's legitimate `new()` call (T3) arrives and fails with `ERR_ALREADY_INITIALIZED`.
6. Attacker, now `owner_id`, calls `stage_upgrade` with malicious WASM, then calls `deploy_upgrade` (open to anyone) after 0 blocks delay.
7. Malicious WASM drains all bridged ETH and token balances from the engine. [1](#0-0) [2](#0-1)

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

**File:** engine/src/contract_methods/admin.rs (L103-121)
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

**File:** etc/xcc-router/src/lib.rs (L69-76)
```rust
        // The first time this function is called there is no state and the parent is set to be
        // the predecessor account id. In subsequent calls, only the original parent is allowed to
        // call this function. The idea is that the Create, Deploy and Initialize actions are done in a single
        // NEAR batch when a new router is deployed by the engine, so the caller will be the Aurora
        // engine instance that the user's address belongs to. If we update this contract and deploy
        // a new version of it, again the Deploy and Initialize actions will be done in a single batch
        // by the engine.
        let caller = env::predecessor_account_id();
```
