### Title
Unbounded Loop in `move_to_block` Exhausts NEAR Gas Limit, Permanently Freezing Engine Funds - (`engine-hashchain/src/hashchain.rs`)

---

### Summary

`Hashchain::move_to_block` contains an unbounded `while` loop that iterates once per NEAR block since the last engine transaction. When the Aurora Engine is idle for a sufficient number of blocks, the first subsequent transaction exhausts the NEAR 300 TGas per-transaction gas limit inside this loop. Because the hashchain state is only persisted on success, the engine becomes permanently stuck: every future transaction hits the same gas wall, freezing all user funds with no on-chain recovery path.

---

### Finding Description

`move_to_block` in `engine-hashchain/src/hashchain.rs` iterates from `current_block_height` to `next_block_height`, calling `compute_block_hashchain` — which performs a `keccak` hash over a concatenated byte buffer — on every iteration:

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

There is no gas check, no iteration cap, and no early-exit mechanism.

This function is called by `load_hashchain` in `engine/src/hashchain.rs`:

```rust
if let Some(hashchain) = maybe_hashchain.as_mut()
    && block_height > hashchain.get_current_block_height()
{
    hashchain.move_to_block(block_height)?;
}
``` [2](#0-1) 

`load_hashchain` is called by both `with_hashchain` and `with_logs_hashchain`, which wrap **every** engine transaction submission: [3](#0-2) [4](#0-3) 

The hashchain state is only written back to storage on success via `save_hashchain`. If `move_to_block` panics due to gas exhaustion, the state is never updated, so every subsequent transaction re-enters the same loop with the same (or larger) gap and fails identically. [5](#0-4) 

The hashchain is only active when it has been initialized (i.e., `read_current_hashchain` returns `Some`): [6](#0-5) 

Once enabled, any idle period long enough to exceed the per-transaction gas budget triggers the freeze.

---

### Impact Explanation

**Permanent freezing of funds.** Once the gas wall is hit:

- Every call to `submit`, `call`, or any hashchain-wrapped method panics before any state change.
- The hashchain's `current_block_height` is never advanced in storage.
- The gap grows with each passing block, making recovery impossible without a contract upgrade.
- All ETH and ERC-20 balances held by the Aurora Engine contract are inaccessible to users.

This matches the **Critical — Permanent freezing of funds** impact tier.

---

### Likelihood Explanation

NEAR produces approximately one block per second. The NEAR per-transaction gas limit is 300 TGas. Each iteration of `move_to_block` calls `compute_block_hashchain`, which performs:

1. A Merkle tree hash (`compute_hash`)
2. A byte-array concatenation
3. One `keccak` call [7](#0-6) 

If `keccak` is dispatched as a NEAR host function (≈1 TGas each), the engine freezes after **≈300 idle blocks ≈ 5 minutes**. Even if it is a pure-WASM implementation (≈0.001 TGas each), the engine freezes after **≈300,000 idle blocks ≈ 3.5 days** — a realistic idle window for any production deployment. No attacker action is required; normal low-activity periods suffice.

---

### Recommendation

1. **Cap iterations**: Add a maximum step count per call (e.g., 100 blocks) and return a distinct error if the gap exceeds it, allowing the admin to call a dedicated "advance hashchain" method in batches.
2. **Lazy/skip approach**: Instead of hashing every intermediate empty block, record only the block height delta and compute a single aggregated hash, eliminating the linear loop entirely.
3. **Gas guard**: Check `env::used_gas()` inside the loop and break early with a partial-advance error before gas is exhausted, allowing the state to be saved incrementally.

---

### Proof of Concept

1. Deploy Aurora Engine with hashchain enabled (hashchain state initialized in storage).
2. Allow the engine to be idle for N NEAR blocks (N chosen to exceed the gas budget per iteration).
3. Submit any valid EVM transaction via `submit`.
4. `with_logs_hashchain` → `load_hashchain` → `move_to_block` enters the `while` loop.
5. After N iterations of `compute_block_hashchain` (each calling `keccak`), the NEAR runtime terminates execution with a gas-exceeded panic.
6. `save_hashchain` is never reached; storage is unchanged.
7. Every subsequent `submit` call repeats steps 4–6 with an equal or larger gap.
8. The engine is permanently frozen; all user funds are inaccessible. [8](#0-7) [9](#0-8)

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

**File:** engine-hashchain/src/hashchain.rs (L251-266)
```rust
    pub fn add_tx(&mut self, method_name: &str, input: &[u8], output: &[u8], log_bloom: &Bloom) {
        let data = [
            &saturating_cast(method_name.len()).to_be_bytes(),
            method_name.as_bytes(),
            &saturating_cast(input.len()).to_be_bytes(),
            input,
            &saturating_cast(output.len()).to_be_bytes(),
            output,
        ]
        .concat();

        let tx_hash = keccak(&data).0;

        self.txs_logs_bloom.accrue_bloom(log_bloom);
        self.txs_merkle_tree.add(tx_hash);
    }
```

**File:** engine-hashchain/src/hashchain.rs (L269-289)
```rust
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

**File:** engine/src/hashchain.rs (L98-107)
```rust
pub fn read_current_hashchain<I: IO>(io: &I) -> Result<Option<Hashchain>, ContractError> {
    let key = storage::bytes_to_key(KeyPrefix::Hashchain, HASHCHAIN_STATE);
    let maybe_hashchain = io.read_storage(&key).map_or(Ok(None), |value| {
        let bytes = value.to_vec();
        Hashchain::try_deserialize(&bytes)
            .map(Some)
            .map_err(|_| BlockchainHashchainError::DeserializationFailed)
    })?;
    Ok(maybe_hashchain)
}
```

**File:** engine/src/hashchain.rs (L109-116)
```rust
pub fn save_hashchain<I: IO>(io: &mut I, hashchain: &Hashchain) -> Result<(), ContractError> {
    let key = storage::bytes_to_key(KeyPrefix::Hashchain, HASHCHAIN_STATE);
    let bytes = hashchain
        .try_serialize()
        .map_err(|_| BlockchainHashchainError::SerializationFailed)?;
    io.write_storage(&key, &bytes);
    Ok(())
}
```
