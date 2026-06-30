### Title
Permissionless `new` Initialization Allows Any Caller to Seize Engine Ownership Before Legitimate Deployer - (File: engine/src/contract_methods/admin.rs)

### Summary
The `new` contract method in the Aurora Engine is a permissionless NEAR contract entrypoint that initializes all critical engine state, including the `owner_id`. It contains no caller authentication — only a guard that rejects re-initialization once state is set. If the contract is deployed without calling `new` in the same atomic batch, any NEAR account can race to call `new` first, set themselves as owner, and subsequently gain full administrative control over the engine, enabling direct theft of all bridged user funds.

### Finding Description

The `new` function in `engine/src/contract_methods/admin.rs` performs a single guard:

```rust
if state::get_state(&io).is_ok() {
    return Err(b"ERR_ALREADY_INITIALIZED".into());
}
``` [1](#0-0) 

There is no check on `env.predecessor_account_id()`. Any NEAR account that calls this method before the state is written wins the initialization race and becomes the `owner_id` stored in `EngineState`. [2](#0-1) 

This method is exposed as a public NEAR contract entrypoint with no access restriction:

```rust
#[unsafe(no_mangle)]
pub extern "C" fn new() {
    let io = Runtime;
    let env = Runtime;
    contract_methods::admin::new(io, &env)
        .map_err(ContractError::msg)
        .sdk_unwrap();
}
``` [3](#0-2) 

The workspace deployment helper shows that contract deployment and `new` are issued as **separate transactions**, not a single atomic batch:

```rust
let contract = account.deploy(&self.code.ok_or_else(|| ...)?).await?;
// ...
engine.new(self.chain_id, self.owner_id, self.upgrade_delay_blocks)
    .transact()
    .await?;
``` [4](#0-3) 

Between the `deploy` receipt and the `new` receipt, the contract is live on-chain with no state. Any NEAR account observing the chain can submit a `new` call in this window.

The `owner_id` set by `new` gates all privileged operations, including `attach_full_access_key`: [5](#0-4) 

An attacker who wins the `new` race becomes the internal owner and can call `attach_full_access_key` to add their own NEAR full-access key to the Aurora Engine account. With a NEAR full-access key, the attacker can deploy arbitrary contract code to the `aurora` account and drain all bridged ETH and ERC-20 tokens.

### Impact Explanation

**Critical — Direct theft of all user funds.**

The Aurora Engine holds all bridged ETH and NEP-141 tokens on behalf of EVM users. An attacker who seizes ownership via the `new` race can:
1. Call `attach_full_access_key` (owner-only) to add their NEAR key to the engine account.
2. Use that key to deploy malicious WASM that bypasses all accounting and transfers all stored assets to the attacker.

This is equivalent to the external report's scenario: a permissionless initialization function that sets a privileged role (oracle/owner) can be frontrun, leading to complete financial loss for the pool/engine.

### Likelihood Explanation

**Low-Medium.** NEAR does not have a public mempool, so classical EVM-style frontrunning is not possible. However:
- NEAR blocks are finalized in ~1 second and are publicly observable.
- An attacker monitoring the chain for new Aurora Engine deployments (e.g., new silo instances, testnet deployments, or re-deployments after upgrades) can detect the window between `DeployContract` and `new` and submit their own `new` call in the next block.
- The workspace deployment code confirms that separate transactions are used in practice, creating this window.
- NEAR does not have traditional reorgs (BFT consensus), but the race window between two sequential transactions is real and exploitable by any on-chain observer.

### Recommendation

The `new` call must be included in the **same atomic batch** as the `DeployContract` action, using `promise_batch_action_function_call` after `promise_batch_action_deploy_contract`. This is the same pattern already used correctly for the XCC router: [6](#0-5) 

Alternatively, `new` should verify that `env.predecessor_account_id() == env.current_account_id()` (i.e., only a self-call from within a deployment batch is accepted), mirroring the XCC router's own self-call allowance in `initialize`.

### Proof of Concept

1. Attacker monitors the NEAR blockchain for a new Aurora Engine contract deployment (e.g., via an indexer watching `DeployContract` actions on known account patterns).
2. Attacker observes a `DeployContract` receipt on `aurora` (or a silo account) with no subsequent `new` call in the same receipt.
3. In the next block, attacker submits:
   ```
   aurora.new({chain_id: ..., owner_id: "attacker.near", upgrade_delay_blocks: 0})
   ```
4. Attacker's `new` call succeeds; `EngineState.owner_id = "attacker.near"` is written.
5. Legitimate deployer's subsequent `new` call returns `ERR_ALREADY_INITIALIZED` and fails.
6. Attacker calls `aurora.attach_full_access_key({public_key: <attacker_key>})` — succeeds because attacker is owner.
7. Attacker uses their NEAR full-access key to deploy malicious WASM to `aurora`, draining all bridged user funds. [7](#0-6)

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

**File:** engine-workspace/src/lib.rs (L107-127)
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

        Ok(engine)
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
