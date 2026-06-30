### Title
Unbounded Loop in `move_to_block` Causes Permanent DoS When Engine Is Dormant - (File: `engine-hashchain/src/hashchain.rs`)

---

### Summary

The `move_to_block` function in `engine-hashchain/src/hashchain.rs` contains an unbounded `while` loop that iterates once per NEAR block between the stored hashchain block height and the current block height. If the Aurora Engine is not called for a sufficient number of NEAR blocks, this loop exhausts the NEAR transaction gas limit on every subsequent call, permanently freezing the engine's ability to process EVM transactions.

---

### Finding Description

The `Hashchain::move_to_block` function is called every time a transaction is processed through `with_hashchain` or `with_logs_hashchain` in `engine/src/hashchain.rs`. Its purpose is to advance the hashchain state from the last recorded block to the current NEAR block height.

The loop in question:

```rust
// engine-hashchain/src/hashchain.rs, lines 75–85
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

Each iteration calls `compute_block_hashchain`, which performs a `keccak` hash over a concatenated byte slice. The number of iterations equals `next_block_height − stored_block_height` — a value that grows unboundedly with engine dormancy.

The call chain from any EVM transaction entry point is:

1. `with_hashchain` / `with_logs_hashchain` (`engine/src/hashchain.rs`, lines 21–86)
2. → `load_hashchain` (`engine/src/hashchain.rs`, lines 88–96)
3. → `hashchain.move_to_block(block_height)` (`engine-hashchain/src/hashchain.rs`, line 93) [1](#0-0) [2](#0-1) 

Critically, the hashchain state is only written **after** `move_to_block` returns successfully:

```rust
// engine/src/hashchain.rs, lines 39–49
if let Some(mut hashchain) = maybe_hashchain {
    ...
    hashchain.move_to_block(block_height)?;
    ...
    save_hashchain(&mut io, &hashchain)?;
}
``` [3](#0-2) 

If NEAR gas is exhausted inside the loop, the transaction is aborted by the NEAR runtime before `save_hashchain` is ever reached. The stored block height is never updated. Every subsequent transaction faces the same (now larger) block gap and also fails. The condition is self-reinforcing and permanent.

---

### Impact Explanation

Once the block gap between the stored hashchain height and the current NEAR block height exceeds the threshold at which the loop exhausts the NEAR gas limit (~300 TGas), **every EVM transaction submitted to the engine fails**. This includes:

- ETH and ERC-20 transfers
- Withdrawals via the ETH connector
- Any user interaction with EVM contracts

Because the hashchain state is never updated on a failed transaction, the gap only grows with each passing block, making recovery impossible without an admin intervention path that bypasses the hashchain update. This constitutes **permanent freezing of user funds**.

---

### Likelihood Explanation

NEAR produces approximately one block per second. The NEAR gas limit per transaction is 300 TGas. Each loop iteration performs a `keccak` hash over a small buffer; NEAR charges roughly 1–5 TGas per keccak call. This means a block gap of **60–300 blocks (1–5 minutes of engine dormancy)** is sufficient to trigger the DoS.

Engine dormancy can arise from:
- A NEAR network disruption or shard stall
- A period of zero user activity on a less-trafficked Aurora deployment
- Any other condition that prevents transactions from landing for a few minutes

Once the threshold is crossed, the engine cannot self-recover. Likelihood is **Medium** given that even brief network disruptions can trigger it.

---

### Recommendation

Replace the per-block iteration with a single-step computation that does not scale linearly with the block gap. Options include:

1. **Skip empty blocks**: If no transactions occurred in the skipped blocks, their hashchain contribution is deterministic (empty block hash). Compute the result mathematically in O(1) rather than iterating O(N) times.
2. **Cap the loop**: Limit the number of iterations per call and allow the hashchain to catch up across multiple transactions.
3. **Lazy advancement**: Only advance the hashchain by one block per transaction call, storing the target height separately.

---

### Proof of Concept

1. Deploy Aurora Engine with hashchain enabled (hashchain state initialized in storage).
2. Allow the NEAR network to produce ~300 blocks (~5 minutes) with no calls to the engine.
3. Submit any EVM transaction (e.g., a simple ETH transfer via `submit`).
4. The call enters `with_logs_hashchain` → `load_hashchain` → `move_to_block(current_height)`.
5. The `while` loop at line 75 of `engine-hashchain/src/hashchain.rs` iterates ~300 times, each calling `keccak`, exhausting the 300 TGas NEAR limit.
6. The NEAR runtime aborts the transaction. `save_hashchain` is never reached.
7. The stored block height remains at the old value. The gap is now 301 blocks.
8. Every subsequent transaction repeats steps 4–7 with an ever-growing gap.
9. All user funds in the engine are permanently inaccessible. [4](#0-3) [5](#0-4) [6](#0-5)

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
