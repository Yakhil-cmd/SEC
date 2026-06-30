### Title
Unbounded `move_to_block` Loop in Hashchain Causes NEAR Gas Exhaustion, Temporarily Freezing All Transactions - (File: engine-hashchain/src/hashchain.rs)

### Summary
When the Aurora Engine hashchain feature is active, every `submit` call invokes `move_to_block` to advance the hashchain from its stored block height to the current NEAR block height. This function contains an unbounded `while` loop that iterates once per missed block, computing a keccak hash each iteration. After a sufficiently long idle period, the loop exceeds the NEAR 300 Tgas gas limit, causing every subsequent `submit` to fail. Because the hashchain state is only persisted after the loop completes, the hashchain remains stuck, and no user transaction can succeed until an admin resets the hashchain.

### Finding Description

`Hashchain::move_to_block` in `engine-hashchain/src/hashchain.rs` iterates from `self.current_block_height` to `next_block_height` with no upper bound and no gas guard:

```rust
while self.current_block_height < next_block_height {
    self.previous_block_hashchain = self.block_hashchain_computer.compute_block_hashchain(...);
    self.block_hashchain_computer.clear_txs();
    self.current_block_height += 1;
}
``` [1](#0-0) 

This function is called unconditionally by `load_hashchain` in `engine/src/hashchain.rs` on every invocation of `with_logs_hashchain`:

```rust
if let Some(hashchain) = maybe_hashchain.as_mut()
    && block_height > hashchain.get_current_block_height()
{
    hashchain.move_to_block(block_height)?;
}
``` [2](#0-1) 

`with_logs_hashchain` wraps every `submit` entrypoint: [3](#0-2) 

The hashchain state is written to storage only after the loop finishes: [4](#0-3) 

If the NEAR runtime aborts the transaction mid-loop due to gas exhaustion, the storage write never executes. The hashchain remains at the old block height, so every subsequent `submit` re-enters the same loop and fails identically, creating a self-reinforcing freeze.

### Impact Explanation

**High — Temporary freezing of funds.** All user `submit` transactions fail. No EVM transfers, DeFi interactions, or contract calls can execute on Aurora until an admin resets the hashchain via `start_hashchain`. User funds are inaccessible for the duration of the freeze.

### Likelihood Explanation

**Medium.** The hashchain must be explicitly enabled via `start_hashchain` (an admin action). Once enabled, the vulnerability activates whenever the engine is idle long enough for the block gap to exceed the gas budget. NEAR produces ~1 block/second; at conservative gas estimates, a gap of roughly 10,000–30,000 blocks (~3–8 hours of inactivity) is sufficient to exhaust 300 Tgas. Idle periods of this length are realistic during NEAR network slowdowns, low-traffic windows, or after the hashchain is first started on a live deployment.

### Recommendation

Cap the number of blocks processed per call in `move_to_block`. If `next_block_height - current_block_height` exceeds a safe threshold (e.g., 1,000 blocks), either:
- Process only up to the threshold and store the intermediate state, requiring multiple transactions to catch up; or
- Skip empty blocks in bulk by advancing `current_block_height` directly to `next_block_height` when no transactions were recorded in the gap (since empty blocks produce a deterministic hash that can be computed in O(1) with a closed-form or iterative-but-bounded approach).

Add a comment documenting the maximum expected block gap and the gas budget assumption.

### Proof of Concept

1. Admin calls `start_hashchain` — hashchain is initialized at block height `N`.
2. Aurora Engine is idle for ~10,000 NEAR blocks (~3 hours).
3. Any unprivileged user submits an EVM transaction via `submit`.
4. `with_logs_hashchain` → `load_hashchain` → `move_to_block(N + 10_000)`.
5. The `while` loop executes 10,000 iterations, each calling `compute_block_hashchain` (keccak over ~100 bytes).
6. NEAR runtime exhausts the 300 Tgas budget mid-loop; transaction is aborted.
7. Hashchain storage is unchanged — still at block `N`.
8. Every subsequent `submit` from any user repeats steps 4–7 and fails identically.
9. All user funds on Aurora are inaccessible until the admin calls `start_hashchain` to reset the hashchain to the current block height. [5](#0-4) [6](#0-5)

### Citations

**File:** engine-hashchain/src/hashchain.rs (L67-88)
```rust
    pub fn move_to_block(
        &mut self,
        next_block_height: u64,
    ) -> Result<(), BlockchainHashchainError> {
        if next_block_height <= self.current_block_height {
            return Err(BlockchainHashchainError::BlockHeightIncorrect);
        }

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

        Ok(())
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

**File:** engine/src/hashchain.rs (L88-96)
```rust
fn load_hashchain<I: IO>(io: &I, block_height: u64) -> Result<Option<Hashchain>, ContractError> {
    let mut maybe_hashchain = read_current_hashchain(io)?;
    if let Some(hashchain) = maybe_hashchain.as_mut()
        && block_height > hashchain.get_current_block_height()
    {
        hashchain.move_to_block(block_height)?;
    }
    Ok(maybe_hashchain)
}
```

**File:** engine/src/contract_methods/evm_transactions.rs (L73-103)
```rust
#[named]
pub fn submit<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let tx_data = io.read_input().to_vec();
        let current_account_id = env.current_account_id();
        let relayer_address = predecessor_address(&env.predecessor_account_id());
        let args = SubmitArgs {
            tx_data,
            ..Default::default()
        };
        let result = engine::submit(
            io,
            env,
            &args,
            state,
            current_account_id,
            relayer_address,
            handler,
        )?;
        let result_bytes = borsh::to_vec(&result).map_err(|_| errors::ERR_SERIALIZE)?;
        io.return_output(&result_bytes);

        Ok(result)
    })
}
```
