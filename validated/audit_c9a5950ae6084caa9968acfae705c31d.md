### Title
Missing Upper-Bound Block Check on `deploy_upgrade` Allows Any Caller to Force Deployment of a Staged Upgrade Indefinitely - (File: engine/src/lib.rs)

### Summary

`deploy_upgrade` enforces only a lower-bound block-height guard (`block_height <= index` → too early) but has no upper-bound expiry. Combined with the absence of any `require_owner_only` check, any unprivileged NEAR account can trigger deployment of a staged upgrade at any point after the delay window opens — including after the owner has decided to abort the upgrade.

### Finding Description

`stage_upgrade` (owner-only) stores the new contract bytecode and a `delay_block_height = current_block + upgrade_delay_blocks` in storage. [1](#0-0) 

`deploy_upgrade` then reads that stored index and enforces only one guard:

```rust
if io.block_height() <= index {
    sdk::panic_utf8(errors::ERR_NOT_ALLOWED_TOO_EARLY);
}
``` [2](#0-1) 

There is no `require_owner_only` call and no upper-bound expiry check anywhere in `deploy_upgrade`. The function is a public `extern "C"` entry point callable by any NEAR account. [3](#0-2) 

There is also no `cancel_upgrade` or equivalent function in the contract, so once an upgrade is staged the owner has no on-chain mechanism to revoke it. [4](#0-3) 

### Impact Explanation

If the owner stages an upgrade and subsequently discovers a critical bug in the new bytecode (or simply changes their mind), they cannot prevent deployment. Any unprivileged NEAR account can call `deploy_upgrade` at any block after the delay window opens — days, weeks, or months later — and force the buggy code live. A deployed contract with a critical bug can cause **permanent freezing of user funds** or **direct theft of funds** held in the Aurora EVM.

### Likelihood Explanation

The owner staging an upgrade is a routine operational event. An attacker monitoring the NEAR chain for a `CODE_STAGE_KEY` write can detect the staged upgrade immediately and wait for the delay to expire. Because there is no expiry, the attacker has an unlimited window to execute. The owner has no on-chain recourse once the delay passes.

### Recommendation

1. Add an expiry block height stored alongside `delay_block_height` in `stage_upgrade` (e.g., `delay_block_height + MAX_DEPLOY_WINDOW`).
2. In `deploy_upgrade`, add a second guard:
   ```rust
   if io.block_height() > expiry_index {
       sdk::panic_utf8(errors::ERR_NOT_ALLOWED_TOO_LATE);
   }
   ```
3. Add `require_owner_only` to `deploy_upgrade`, or add a `cancel_upgrade` function (owner-only) that clears `CODE_KEY` and `CODE_STAGE_KEY`.

### Proof of Concept

1. Owner calls `stage_upgrade` with new (buggy) bytecode. `CODE_STAGE_KEY` is written with `delay_block_height = N + upgrade_delay_blocks`. [5](#0-4) 
2. Owner discovers the bug and wants to abort. There is no `cancel_upgrade` function.
3. After block `N + upgrade_delay_blocks`, any NEAR account calls `deploy_upgrade`. The only check `block_height <= index` passes (it is now `>`), so `Runtime::self_deploy` executes the buggy bytecode. [6](#0-5) 
4. The buggy contract is now live. Depending on the bug, user funds in the Aurora EVM can be permanently frozen or stolen.

### Citations

**File:** engine/src/contract_methods/admin.rs (L50-52)
```rust
const CODE_KEY: &[u8; 4] = b"CODE";
const CODE_STAGE_KEY: &[u8; 10] = b"CODE_STAGE";
const GAS_FOR_STATE_MIGRATION: NearGas = NearGas::new(50_000_000_000_000);
```

**File:** engine/src/contract_methods/admin.rs (L154-167)
```rust
pub fn stage_upgrade<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let delay_block_height = env.block_height() + state.upgrade_delay_blocks;
        require_owner_only(&state, &env.predecessor_account_id())?;
        io.read_input_and_store(&storage::bytes_to_key(KeyPrefix::Config, CODE_KEY));
        io.write_storage(
            &storage::bytes_to_key(KeyPrefix::Config, CODE_STAGE_KEY),
            &delay_block_height.to_le_bytes(),
        );
        Ok(())
    })
}
```

**File:** engine/src/lib.rs (L171-185)
```rust
    pub extern "C" fn deploy_upgrade() {
        // This function is intentionally not implemented in `contract_methods`
        // because it only makes sense in the context of the NEAR runtime.
        let mut io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_running(&state)
            .map_err(ContractError::msg)
            .sdk_unwrap();
        let index = internal_get_upgrade_index();
        if io.block_height() <= index {
            sdk::panic_utf8(errors::ERR_NOT_ALLOWED_TOO_EARLY);
        }
        Runtime::self_deploy(&bytes_to_key(KeyPrefix::Config, CODE_KEY));
        io.remove_storage(&bytes_to_key(KeyPrefix::Config, CODE_STAGE_KEY));
    }
```
