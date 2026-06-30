### Title
Unbounded Loop in `move_to_block` Causes NEAR Gas Exhaustion and Permanent Fund Freeze - (File: engine-hashchain/src/hashchain.rs)

### Summary
The `Hashchain::move_to_block` function contains an unbounded `while` loop that iterates once per NEAR block between the last recorded hashchain block height and the current block height. When the gap between the last hashchain update and the current block is large enough, this loop exhausts NEAR gas on every subsequent EVM transaction, permanently freezing all user funds in the engine.

### Finding Description
`Hashchain::move_to_block` iterates from `self.current_block_height` to `next_block_height` with no upper bound or pagination: [1](#0-0) 

Each iteration calls `compute_block_hashchain`, which performs a `keccak` hash over a concatenated byte slice: [2](#0-1) 

This function is invoked unconditionally from `load_hashchain` whenever the stored hashchain block height lags behind the current NEAR block height: [3](#0-2) 

`load_hashchain` is called at the start of every `with_hashchain` and `with_logs_hashchain` wrapper, which wraps all EVM transaction submission paths: [4](#0-3) 

NEAR produces approximately one block per second. If no EVM transactions are submitted for an extended period (e.g., a few weeks of inactivity, or a deliberate pause), the gap `next_block_height - current_block_height` grows proportionally. Once this gap is large enough that the loop's cumulative NEAR gas cost exceeds the NEAR per-transaction gas limit (~300 TGas), every call to `with_hashchain` fails. Because the hashchain state in storage is only updated on success, the stored `current_block_height` never advances, and the gap continues to grow with each passing block. Every subsequent EVM transaction fails for the same reason, with an ever-increasing gap.

### Impact Explanation
**Critical — Permanent freezing of funds.**

Once the block gap crosses the NEAR gas threshold, no EVM transaction can be processed: `submit`, `ft_on_transfer`, withdrawals, and all other hashchain-wrapped methods all fail before any EVM execution occurs. User ETH, ERC-20 tokens, and bridged assets held in the Aurora engine become permanently inaccessible. Recovery requires an admin path that does not go through `with_hashchain`; if no such path exists or if it also wraps through the hashchain, the freeze is irrecoverable without a contract upgrade.

### Likelihood Explanation
The hashchain feature must be initialized (opt-in), but once active it is always consulted. A gap of roughly 3–10 million NEAR blocks (35–115 days of inactivity) is sufficient to exhaust 300 TGas given the per-iteration keccak cost. Periods of low activity, planned maintenance, or a deliberate griefing strategy (e.g., a relayer going offline) can produce this gap without any privileged access. Any unprivileged EVM user or relayer who submits the first transaction after such a gap triggers the condition and discovers the freeze.

### Recommendation
Cap the number of iterations in `move_to_block` per call, or split the catch-up work across multiple NEAR transactions. A simple fix is to add a maximum step size:

```rust
const MAX_BLOCKS_PER_CALL: u64 = 10_000;
let target = next_block_height.min(self.current_block_height + MAX_BLOCKS_PER_CALL);
while self.current_block_height < target { ... }
if self.current_block_height < next_block_height {
    return Err(BlockchainHashchainError::TooManyBlocksToSkip);
}
```

Alternatively, store only the delta (skip empty blocks without iterating) or use a lazy/checkpoint approach that does not require iterating every intermediate block.

### Proof of Concept

1. Initialize the Aurora Engine with the hashchain feature enabled.
2. Submit one EVM transaction to anchor the hashchain at block height `B`.
3. Allow `N` NEAR blocks to pass without any EVM transactions (no relayer activity), where `N` is large enough that `N × cost_per_keccak_iteration > 300 TGas`.
4. Submit any EVM transaction (e.g., a simple ETH transfer). The call enters `with_hashchain` → `load_hashchain` → `move_to_block(B + N)`.
5. The `while` loop runs `N` iterations, each executing `compute_block_hashchain` (keccak over ~100+ bytes). NEAR gas is exhausted before the loop completes; the transaction fails with a gas error.
6. The hashchain in storage still records block height `B`. The next block is now `B + N + 1`, so the gap is `N + 1`. Every subsequent EVM transaction fails with an even larger gap.
7. All user funds are permanently frozen. [5](#0-4) [6](#0-5)

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
