### Title
Unbounded `move_to_block` Loop in `Hashchain` Causes Gas Exhaustion After Inactivity, Freezing the Engine - (File: `engine-hashchain/src/hashchain.rs`, `engine/src/hashchain.rs`)

### Summary
The `Hashchain::move_to_block` function iterates through every NEAR block skipped since the last processed transaction, calling `keccak` once per block. This is invoked unconditionally on every hashchain-wrapped transaction via `load_hashchain`. If the engine is inactive for a sufficiently large number of blocks, the accumulated keccak calls exhaust the 300 TGas NEAR gas budget, causing every subsequent transaction to fail and freezing the engine until an admin resets the hashchain.

### Finding Description
`load_hashchain` in `engine/src/hashchain.rs` is called at the start of every transaction wrapped by `with_hashchain` or `with_logs_hashchain`:

```rust
fn load_hashchain<I: IO>(io: &I, block_height: u64) -> Result<Option<Hashchain>, ContractError> {
    let mut maybe_hashchain = read_current_hashchain(io)?;
    if let Some(hashchain) = maybe_hashchain.as_mut()
        && block_height > hashchain.get_current_block_height()
    {
        hashchain.move_to_block(block_height)?;   // ← unbounded loop
    }
    Ok(maybe_hashchain)
}
```

`move_to_block` in `engine-hashchain/src/hashchain.rs` contains an unbounded `while` loop:

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

Each iteration calls `compute_block_hashchain`, which calls `aurora_engine_sdk::keccak` (the NEAR host function `keccak256`) on ~366 bytes of data. There is no cap on the number of iterations. The number of iterations equals the number of NEAR blocks elapsed since the last transaction was processed.

This is structurally identical to the TokenFaucet pattern: a stored "last processed" counter (`current_block_height` ↔ `lastDripTimestamp`) grows stale during inactivity, and the first action after a gap triggers an unbounded computation proportional to the elapsed time.

### Impact Explanation
When the engine has been idle for N blocks, the first transaction submitted by any user triggers N keccak host-function calls inside `move_to_block`. Each NEAR `keccak256` call costs approximately 1.5 TGas base + ~0.55 TGas for the ~366-byte input ≈ 2 TGas, plus the `StreamCompactMerkleTree::compute_hash` call adds another ~2 TGas per iteration. At ~4 TGas per iteration and a 300 TGas budget shared with EVM execution and storage I/O, the hashchain catch-up alone can exhaust the budget after roughly 50–75 idle blocks (~50–75 seconds on NEAR mainnet). Once this threshold is crossed, every transaction fails before EVM execution, preventing all withdrawals of ETH and ERC-20 tokens from the Aurora EVM. This constitutes **temporary freezing of funds** (High).

The freeze persists until the key manager calls `start_hashchain` with `args.block_height` set to `current_block_height - 1`, which skips the loop entirely. However, if `pause_contract` is also wrapped by `with_hashchain` (a pattern consistent with the rest of the codebase), the key manager cannot pause the contract to invoke `start_hashchain`, potentially escalating to a permanent freeze.

### Likelihood Explanation
NEAR produces approximately one block per second. An idle period of ~50–75 seconds is realistic on testnets, newly deployed instances, or during low-traffic windows. The hashchain feature is opt-in (`initial_hashchain` in `NewCallArgsV4` or via `start_hashchain`), but once enabled it is always active. No special attacker capability is required; any ordinary EVM user submitting the first transaction after an idle period triggers the condition.

### Recommendation
Cap the number of iterations in `move_to_block` to a safe maximum (e.g., 30–40 blocks per call), and if more blocks must be skipped, defer the remaining catch-up to subsequent transactions. Alternatively, store only the last processed block height and compute the hashchain lazily for the current block only, discarding intermediate empty-block hashes (since they contain no transactions and are deterministic).

### Proof of Concept
1. Deploy Aurora Engine with `initial_hashchain` set (hashchain enabled).
2. Submit one transaction to confirm the engine is live; note `current_block_height = H`.
3. Allow the engine to sit idle for ≥ 75 NEAR blocks (~75 seconds).
4. Submit any EVM transaction (e.g., a simple ETH transfer).
5. `load_hashchain` calls `move_to_block(H + 75)`, executing 75 keccak calls ≈ 300 TGas, exhausting the gas budget before EVM execution begins.
6. The transaction fails with an out-of-gas error; the hashchain state is not updated.
7. All subsequent transactions fail identically; the engine is frozen. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** engine-hashchain/src/hashchain.rs (L268-289)
```rust
    /// Computes the block hashchain.
    pub fn compute_block_hashchain(
        &self,
        chain_id: &[u8; 32],
        contract_account_id: &[u8],
        current_block_height: u64,
        previous_block_hashchain: RawH256,
    ) -> RawH256 {
        let txs_hash = self.txs_merkle_tree.compute_hash();

        let data = [
            chain_id,
            contract_account_id,
            &current_block_height.to_be_bytes(),
            &previous_block_hashchain,
            &txs_hash,
            self.txs_logs_bloom.as_bytes(),
        ]
        .concat();

        keccak(&data).0
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
