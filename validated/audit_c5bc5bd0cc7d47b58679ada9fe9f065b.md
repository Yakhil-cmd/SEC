### Title
Unguarded `new()` Initializer Allows Any Caller to Seize Engine Ownership Before Legitimate Initialization — (File: `engine/src/contract_methods/admin.rs`)

---

### Summary

The Aurora Engine's `new()` initialization function performs no caller authentication. Any NEAR account can invoke it between contract deployment and the deployer's own initialization call. Because the workspace deployment flow issues deployment and initialization as two separate transactions, a window exists in which an attacker can front-run the `new()` call, supply their own `owner_id`, and permanently seize ownership of the Aurora Engine contract.

---

### Finding Description

The public NEAR entrypoint `new()` in `engine/src/lib.rs` delegates directly to `contract_methods::admin::new()`:

```rust
#[unsafe(no_mangle)]
pub extern "C" fn new() {
    let io = Runtime;
    let env = Runtime;
    contract_methods::admin::new(io, &env)
        .map_err(ContractError::msg)
        .sdk_unwrap();
}
``` [1](#0-0) 

The implementation in `admin.rs` guards only against double-initialization; it performs **no check on `env.predecessor_account_id()`**:

```rust
pub fn new<I: IO + Copy, E: Env>(mut io: I, env: &E) -> Result<(), ContractError> {
    if state::get_state(&io).is_ok() {
        return Err(b"ERR_ALREADY_INITIALIZED".into());
    }
    let input = io.read_input().to_vec();
    let args = NewCallArgs::deserialize(&input)...;
    ...
    state::set_state(&mut io, &state)?;
    Ok(())
}
``` [2](#0-1) 

The `EngineState` written by this call includes the `owner_id` field, which is fully attacker-controlled when the call is front-run.

The workspace deployment helper `deploy_and_init` issues deployment and initialization as **two separate async transactions**:

```rust
let contract = account.deploy(&self.code...).await?;
...
engine.new(self.chain_id, self.owner_id, self.upgrade_delay_blocks)
    .transact()
    .await
``` [3](#0-2) 

Between the `deploy` call settling and the `new` call being submitted, the contract is live on-chain with no state. Any NEAR account that observes the deployment transaction can race to call `new()` first with an attacker-controlled `owner_id`.

---

### Impact Explanation

The `owner_id` stored during `new()` is the root of the engine's privilege model. The owner can subsequently call `set_eth_connector_contract_account`, replacing the legitimate ETH connector with a malicious contract: [4](#0-3) 

A malicious ETH connector can redirect or absorb all bridge deposits and withdrawals, enabling **direct theft of bridged ETH and ERC-20 tokens** from users. At minimum, the attacker-owner can pause precompiles, causing a **temporary freeze of funds** for all users.

**Impact: Critical** — direct theft of bridged user funds via malicious connector substitution.

---

### Likelihood Explanation

NEAR transactions are publicly observable before finality. Any attacker monitoring the `aurora` account for a `DeployContract` action can immediately submit a `new()` call with a crafted payload. The attack requires no special privileges, no leaked keys, and no social engineering — only the ability to submit a NEAR transaction. The deployment-then-initialize two-step pattern is confirmed in the workspace code and is the standard deployment flow.

**Likelihood: High** — trivially executable by any NEAR account watching the chain.

---

### Recommendation

Enforce that `new()` can only be called by the contract account itself (i.e., `env.predecessor_account_id() == env.current_account_id()`), or by a designated deployer key. Alternatively, bundle the `DeployContract` and `FunctionCall { name: "new" }` actions into a single NEAR batch transaction so they execute atomically, eliminating the front-run window entirely. The XCC router already demonstrates the correct pattern — `CreateAccount`, `Transfer`, `DeployContract`, and `FunctionCall { name: "initialize" }` are all issued as a single `PromiseBatchAction`: [5](#0-4) 

---

### Proof of Concept

1. Attacker monitors the NEAR chain for a `DeployContract` action targeting the `aurora` account.
2. Deployment transaction finalizes; the engine WASM is live but `state::get_state()` returns `Err` (no state yet).
3. Attacker immediately submits a NEAR transaction calling `aurora.new()` with:
   - `chain_id`: any valid value
   - `owner_id`: attacker's own NEAR account ID
   - `upgrade_delay_blocks`: 0
4. Because `state::get_state(&io).is_ok()` is `false`, the guard at line 57 passes. [6](#0-5) 
5. `state::set_state` writes the attacker's `owner_id` into the engine's storage. [7](#0-6) 
6. The legitimate deployer's subsequent `new()` call returns `ERR_ALREADY_INITIALIZED` and fails.
7. Attacker, now owner, calls `set_eth_connector_contract_account` pointing to a malicious contract, redirecting all bridge operations and draining bridged user funds. [8](#0-7)

### Citations

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

**File:** engine/src/contract_methods/admin.rs (L56-88)
```rust
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

**File:** engine/src/contract_methods/connector.rs (L1-30)
```rust
use aurora_engine_modexp::AuroraModExp;
use aurora_engine_sdk::env::Env;
use aurora_engine_sdk::io::{IO, StorageIntermediate};
use aurora_engine_sdk::promise::PromiseHandler;
use aurora_engine_types::account_id::AccountId;
use aurora_engine_types::borsh::{self, BorshDeserialize};
use aurora_engine_types::parameters::connector::{
    EngineWithdrawCallArgs, Erc20Identifier, Erc20Metadata, ExitToNearPrecompileCallbackArgs,
    FtOnTransferArgs, FtTransferArgs, FtTransferCallArgs, FungibleTokenMetadata,
    MirrorErc20TokenArgs, SetErc20MetadataArgs, SetEthConnectorContractAccountArgs,
    StorageDepositArgs, StorageUnregisterArgs, StorageWithdrawArgs, WithdrawCallArgs,
    WithdrawSerializeType,
};
use aurora_engine_types::parameters::engine::errors::ParseArgsError;
use aurora_engine_types::parameters::engine::{DeployErc20TokenArgs, SubmitResult};
use aurora_engine_types::parameters::{
    PromiseAction, PromiseBatchAction, PromiseCreateArgs, PromiseOrValue, PromiseWithCallbackArgs,
};
use aurora_engine_types::storage::{EthConnectorStorageId, KeyPrefix};
use aurora_engine_types::types::{Address, NearGas, PromiseResult, Yocto};
use function_name::named;

use crate::contract_methods::{
    ContractError, predecessor_address, require_owner_only, require_running,
};
use crate::engine::Engine;
use crate::hashchain::with_hashchain;
use crate::prelude::{ToString, Vec, sdk, vec};
use crate::{engine, errors, state};

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
