### Title
Unbounded Loop in `Hashchain::move_to_block` Causes Permanent DoS of All State-Changing Engine Operations — (`engine-hashchain/src/hashchain.rs`)

---

### Summary

`Hashchain::move_to_block` contains an unbounded `while` loop that iterates once per NEAR block between the stored `current_block_height` and the actual current NEAR block height. This function is invoked on every state-changing contract call via `with_hashchain` / `with_logs_hashchain`. If the gap between the stored height and the live block height grows large enough, the loop exhausts the NEAR 300 Tgas transaction gas limit, causing every state-changing call to fail. Because `pause_contract` itself goes through `with_hashchain`, even the owner's recovery path is blocked, making the freeze potentially permanent without a contract upgrade.

---

### Finding Description

`Hashchain::move_to_block` in `engine-hashchain/src/hashchain.rs` (lines 67–88) iterates from `self.current_block_height` up to `next_block_height`, performing a keccak hash computation and a Merkle-tree clear on every iteration:

```rust
while self.current_block_height < next_block_height {
    self.previous_block_hashchain = self.block_hashchain_computer.compute_block_hashchain(
        &self.chain_id,
        self.contract_account_id.as_bytes(),
        self.current_block_height,
        self.previous_block_hashchain,
    );
    self.block_hashchain_computer.clear_txs();
    self.current_block_height += 1;
}
``` [1](#0-0) 

This loop is triggered inside `load_hashchain` in `engine/src/hashchain.rs` whenever the stored block height lags behind the current NEAR block height:

```rust
if let Some(hashchain) = maybe_hashchain.as_mut()
    && block_height > hashchain.get_current_block_height()
{
    hashchain.move_to_block(block_height)?;
}
``` [2](#0-1) 

`load_hashchain` is called by both `with_hashchain` and `with_logs_hashchain`: [3](#0-2) [4](#0-3) 

These two wrappers are used by **every** state-changing contract method, including:

- `ft_on_transfer` (deposits) [5](#0-4) 
- `exit_to_near_precompile_callback` (withdrawals) [6](#0-5) 
- `pause_contract` and `resume_contract` (admin recovery) [7](#0-6) 
- EVM transaction submission via `with_logs_hashchain`

NEAR produces approximately one block per second. If the Aurora Engine (or a silo deployment) experiences a period of inactivity — even a few minutes — the gap between the stored `current_block_height` and the live block height grows proportionally. Once the gap is large enough to exhaust 300 Tgas in a single call, every subsequent call through `with_hashchain` fails with out-of-gas. Because the hashchain state is never written on a failed transaction, the gap never shrinks, and the condition is self-reinforcing.

The intended recovery path is `start_hashchain` (which lets the admin supply a fresh `block_height` and resets the hashchain), but `start_hashchain` itself calls `move_to_block`:

```rust
if hashchain.get_current_block_height() < block_height {
    hashchain.move_to_block(block_height)?;
}
``` [8](#0-7) 

And `start_hashchain` requires the contract to be paused first (`require_paused`), but `pause_contract` also goes through `with_hashchain`: [9](#0-8) 

This closes the recovery loop: once the gap is above the gas threshold, neither users nor the owner can execute any state-changing call, and the contract is stuck until a new WASM binary is deployed via a direct NEAR account action.

---

### Impact Explanation

All user funds deposited in the Aurora Engine (ETH, ERC-20 tokens bridged via the connector) become inaccessible. No deposits, withdrawals, EVM calls, or administrative actions can succeed. This constitutes **permanent freezing of funds** in the worst case, or **temporary freezing** if a contract upgrade can be deployed out-of-band. The impact matches the "High – Temporary freezing of funds" and potentially "Critical – Permanent freezing of funds" categories.

---

### Likelihood Explanation

NEAR produces ~1 block/second. Any deployment (especially a silo) that goes idle for an extended period, or that is paused for maintenance and not promptly resumed, will accumulate a large block gap. An unprivileged user does not need to take any action; the gap grows passively. The first user to submit a transaction after the threshold is crossed triggers the DoS for all subsequent users. For the main Aurora deployment the risk is lower due to constant activity, but for silo deployments or after a security pause it is realistic.

---

### Recommendation

Replace the unbounded `while` loop in `move_to_block` with a bounded iteration that processes at most `N` blocks per call (analogous to the `totalRoundsToConsider` parameter added in the referenced report). The caller should be able to invoke the function multiple times to catch up incrementally. Alternatively, store the block height lazily and compute the hashchain only for blocks that actually contained transactions, skipping empty blocks in O(1).

```rust
pub fn move_to_block_bounded(
    &mut self,
    next_block_height: u64,
    max_steps: u64,
) -> Result<bool, BlockchainHashchainError> {
    // returns true when fully caught up
    let limit = next_block_height.min(self.current_block_height + max_steps);
    while self.current_block_height < limit {
        self.previous_block_hashchain = self.block_hashchain_computer.compute_block_hashchain(...);
        self.block_hashchain_computer.clear_txs();
        self.current_block_height += 1;
    }
    Ok(self.current_block_height >= next_block_height)
}
```

---

### Proof of Concept

1. Deploy Aurora Engine with hashchain enabled (call `new` with a non-`None` `initial_hashchain`).
2. Submit no transactions for `K` NEAR blocks (or simulate by advancing the mock block height by `K`).
3. Submit any state-changing call (e.g., `ft_on_transfer`, `submit`).
4. Observe that `load_hashchain` → `move_to_block` iterates `K` times, each performing a keccak hash. For sufficiently large `K` (empirically determinable from NEAR gas metering), the call fails with out-of-gas.
5. Observe that all subsequent calls also fail, including `pause_contract`, confirming the self-reinforcing nature of the freeze.

### Citations

**File:** engine-hashchain/src/hashchain.rs (L75-85)
```rust
        while self.current_block_height < next_block_height {
            self.previous_block_hashchain = self.block_hashchain_computer.compute_block_hashchain(
                &self.chain_id,
                self.contract_account_id.as_bytes(),
                self.current_block_height,
                self.previous_block_hashchain,
            );

            self.block_hashchain_computer.clear_txs();
            self.current_block_height += 1;
        }
```

**File:** engine/src/hashchain.rs (L21-52)
```rust
pub fn with_hashchain<I, E, T, F>(
    mut io: I,
    env: &E,
    function_name: &str,
    f: F,
) -> Result<T, ContractError>
where
    I: IO + Copy,
    E: Env,
    F: for<'a> FnOnce(CachedIO<'a, I>) -> Result<T, ContractError>,
{
    let block_height = env.block_height();
    let maybe_hashchain = load_hashchain(&io, block_height)?;

    let cache = RefCell::new(IOCache::default());
    let hashchain_io = CachedIO::new(io, &cache);
    let result = f(hashchain_io)?;

    if let Some(mut hashchain) = maybe_hashchain {
        let cache_ref = cache.borrow();
        hashchain.add_block_tx(
            block_height,
            function_name,
            &cache_ref.input,
            &cache_ref.output,
            &Bloom::default(),
        )?;
        save_hashchain(&mut io, &hashchain)?;
    }

    Ok(result)
}
```

**File:** engine/src/hashchain.rs (L54-86)
```rust
pub fn with_logs_hashchain<I, E, F>(
    mut io: I,
    env: &E,
    function_name: &str,
    f: F,
) -> Result<SubmitResult, ContractError>
where
    I: IO + Copy,
    E: Env,
    F: for<'a> FnOnce(CachedIO<'a, I>) -> Result<SubmitResult, ContractError>,
{
    let block_height = env.block_height();
    let maybe_hashchain = load_hashchain(&io, block_height)?;

    let cache = RefCell::new(IOCache::default());
    let hashchain_io = CachedIO::new(io, &cache);
    let result = f(hashchain_io)?;

    if let Some(mut hashchain) = maybe_hashchain {
        let log_bloom = bloom::get_logs_bloom(&result.logs);
        let cache_ref = cache.borrow();
        hashchain.add_block_tx(
            block_height,
            function_name,
            &cache_ref.input,
            &cache_ref.output,
            &log_bloom,
        )?;
        save_hashchain(&mut io, &hashchain)?;
    }

    Ok(result)
}
```

**File:** engine/src/hashchain.rs (L89-95)
```rust
    let mut maybe_hashchain = read_current_hashchain(io)?;
    if let Some(hashchain) = maybe_hashchain.as_mut()
        && block_height > hashchain.get_current_block_height()
    {
        hashchain.move_to_block(block_height)?;
    }
    Ok(maybe_hashchain)
```

**File:** engine/src/contract_methods/connector.rs (L62-109)
```rust
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        let mut engine: Engine<_, _> = Engine::new(
            predecessor_address(&predecessor_account_id),
            current_account_id.clone(),
            io,
            env,
        )?;

        sdk::log!("Call ft_on_transfer");

        let args: FtOnTransferArgs = read_json_args(&io)?;
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };

        #[allow(clippy::used_underscore_binding)]
        let amount_to_return = if let Err(_err) = &result {
            sdk::log!("Error in ft_on_transfer: {_err:?}");
            // An error occurred, so we need to return the amount of tokens to the sender.
            args.amount.as_u128()
        } else {
            // Everything is ok, so return 0.
            0
        };

        let output = crate::prelude::format!("\"{amount_to_return}\"");
        io.return_output(output.as_bytes());

        // In case of an error, we just return Ok(None) to avoid a panic in the contract. It's ok
        // because in case of an error, we already returned the amount of tokens to the sender.
        Ok(result.unwrap_or(None))
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L196-246)
```rust
pub fn exit_to_near_precompile_callback<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        env.assert_private_call()?;

        // This function should only be called as the callback of
        // exactly one promise.
        if handler.promise_results_count() != 1 {
            return Err(errors::ERR_PROMISE_COUNT.into());
        }

        let args: ExitToNearPrecompileCallbackArgs = io.read_input_borsh()?;

        let maybe_result = if let Some(PromiseResult::Successful(_)) = handler.promise_result(0) {
            if let Some(args) = args.transfer_near {
                let action = PromiseAction::Transfer {
                    amount: Yocto::new(args.amount),
                };
                let promise = PromiseBatchAction {
                    target_account_id: args.target_account_id,
                    actions: vec![action],
                };

                // Safety: this call is safe because it comes from the exit to near precompile, not users.
                // The call is to transfer the unwrapped wNEAR tokens.
                let promise_id = handler.promise_create_batch(&promise);
                handler.promise_return(promise_id);
            }

            None
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }

            Some(refund_result)
        } else {
            None
        };

        Ok(maybe_result)
    })
}
```

**File:** engine/src/contract_methods/admin.rs (L251-272)
```rust
pub fn pause_contract<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        require_running(&state)?;
        state.is_paused = true;
        state::set_state(&mut io, &state)?;
        Ok(())
    })
}

#[named]
pub fn resume_contract<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        let mut state = state::get_state(&io)?;
        require_owner_only(&state, &env.predecessor_account_id())?;
        require_paused(&state)?;
        state.is_paused = false;
        state::set_state(&mut io, &state)?;
        Ok(())
    })
}
```

**File:** engine/src/contract_methods/admin.rs (L447-449)
```rust
    if hashchain.get_current_block_height() < block_height {
        hashchain.move_to_block(block_height)?;
    }
```
