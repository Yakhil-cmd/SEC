### Title
Unbounded `while` Loop in `move_to_block` Causes Permanent NEAR Gas Exhaustion and Fund Freeze - (File: `engine-hashchain/src/hashchain.rs`)

### Summary

The `Hashchain::move_to_block` function contains an unbounded `while` loop that iterates once per skipped NEAR block. Because this function is called on every EVM transaction submission via `with_logs_hashchain` → `load_hashchain`, a sufficiently long idle period causes every subsequent transaction to exhaust the NEAR 300 TGas limit. Since the hashchain state is only persisted on success, the gap never closes, permanently freezing all EVM transaction processing.

---

### Finding Description

`engine-hashchain/src/hashchain.rs` defines `move_to_block`:

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

Each iteration performs a `keccak` hash over ~360+ bytes of data (chain ID, account ID, block height, previous hashchain, txs hash, 256-byte bloom filter) and resets the bloom filter and Merkle tree. The number of iterations equals `next_block_height − current_block_height`, which is the number of NEAR blocks elapsed since the last transaction.

This function is called unconditionally on every EVM transaction via the call chain:

`submit` / `submit_with_args` / `call` / `deploy_code`
→ `with_logs_hashchain` (`engine/src/hashchain.rs:54`)
→ `load_hashchain` (`engine/src/hashchain.rs:88`)
→ `hashchain.move_to_block(block_height)` (`engine/src/hashchain.rs:93`)

The hashchain state is only written back to storage **after** the loop completes successfully:

```rust
save_hashchain(&mut io, &hashchain)?;  // engine/src/hashchain.rs:82
```

If the NEAR transaction is aborted due to gas exhaustion, no state is committed. The stored hashchain height remains stale, so the next transaction faces an equal or larger gap.

---

### Impact Explanation

**Impact: Permanent freezing of funds.**

Once the block gap grows large enough to exhaust 300 TGas in a single NEAR transaction, every EVM transaction (`submit`, `call`, `deploy_code`) fails at the NEAR level before any EVM execution occurs. Because the hashchain state is never updated on failure, the gap only grows with each passing block. There is no self-healing path: the engine cannot process any transaction, and all user funds locked in Aurora EVM contracts become permanently inaccessible without an emergency contract upgrade.

---

### Likelihood Explanation

**Likelihood: Medium.**

NEAR produces approximately one block per second. The hashchain feature must be explicitly activated via `start_hashchain` (an admin action), but once active it is a prerequisite for every transaction. A gap of ~30,000–50,000 blocks (roughly 8–14 hours of engine inactivity) is sufficient to exhaust 300 TGas given the per-iteration cost of a keccak over ~360 bytes plus bloom-filter and Merkle-tree resets. Engine inactivity of this duration is realistic during maintenance windows, low-traffic periods, or deliberate griefing where an attacker waits for a quiet period and then submits the first transaction to trigger the freeze.

---

### Recommendation

Replace the unbounded `while` loop with a bounded iteration that processes at most `MAX_BLOCKS_PER_TX` empty blocks per call, and return an error (or a partial-progress indicator) if the gap exceeds the bound. Alternatively, store only the block height and defer hashchain computation to a dedicated catch-up method that can be called in multiple transactions. A hard cap on the gap (e.g., reject transactions if the gap exceeds a safe threshold and require an explicit admin catch-up call) would also prevent the freeze.

---

### Proof of Concept

1. Admin calls `start_hashchain` at NEAR block height `H`.
2. The engine receives no transactions for `N` blocks (e.g., `N = 40,000`, ~11 hours).
3. Any user submits an EVM transaction at block height `H + N`.
4. Execution path: `submit` → `with_logs_hashchain` → `load_hashchain` → `hashchain.move_to_block(H + N)`.
5. The `while` loop at [1](#0-0)  executes `N` iterations, each calling `compute_block_hashchain` (a keccak over ~360 bytes) and `clear_txs`.
6. NEAR gas is exhausted before the loop completes; the NEAR transaction is aborted.
7. `save_hashchain` at [2](#0-1)  is never reached; the stored hashchain height remains `H`.
8. Every subsequent transaction repeats steps 3–7 with an ever-growing gap.
9. All EVM transactions are permanently blocked; user funds are frozen.

**Key code references:**

- Unbounded loop: [1](#0-0) 
- Called unconditionally on every EVM tx: [3](#0-2) 
- Hashchain saved only on success (never reached on OOG): [2](#0-1) 
- Entry points wrapping every EVM transaction: [4](#0-3)

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

**File:** engine/src/hashchain.rs (L88-95)
```rust
fn load_hashchain<I: IO>(io: &I, block_height: u64) -> Result<Option<Hashchain>, ContractError> {
    let mut maybe_hashchain = read_current_hashchain(io)?;
    if let Some(hashchain) = maybe_hashchain.as_mut()
        && block_height > hashchain.get_current_block_height()
    {
        hashchain.move_to_block(block_height)?;
    }
    Ok(maybe_hashchain)
```

**File:** engine/src/hashchain.rs (L109-115)
```rust
pub fn save_hashchain<I: IO>(io: &mut I, hashchain: &Hashchain) -> Result<(), ContractError> {
    let key = storage::bytes_to_key(KeyPrefix::Hashchain, HASHCHAIN_STATE);
    let bytes = hashchain
        .try_serialize()
        .map_err(|_| BlockchainHashchainError::SerializationFailed)?;
    io.write_storage(&key, &bytes);
    Ok(())
```

**File:** engine/src/contract_methods/evm_transactions.rs (L74-103)
```rust
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
