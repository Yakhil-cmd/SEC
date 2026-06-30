### Title
Unbounded `move_to_block` Loop in Hashchain Causes Permanent Fund Freeze After Long Pause - (File: `engine-hashchain/src/hashchain.rs`, `engine/src/hashchain.rs`, `engine/src/contract_methods/admin.rs`)

---

### Summary

When the Aurora Engine is paused for a sufficiently long period (measured in NEAR blocks), the `resume_contract` admin call will always revert due to gas exhaustion. This is caused by an unbounded while loop in `Hashchain::move_to_block` that iterates once per NEAR block that elapsed since the last recorded hashchain state. Because `resume_contract` wraps itself in `with_hashchain`, which unconditionally calls `load_hashchain` → `move_to_block` before any state change, the contract becomes permanently unresumable, freezing all user funds held on Aurora.

---

### Finding Description

The Aurora Engine supports a hashchain integrity mechanism. When enabled, every mutating call is wrapped in `with_hashchain` (`engine/src/hashchain.rs:21-52`), which calls `load_hashchain` to bring the in-memory hashchain up to the current NEAR block height before recording the transaction.

`load_hashchain` (`engine/src/hashchain.rs:88-96`) reads the persisted hashchain and, if the current block height is ahead of the stored one, calls `hashchain.move_to_block(block_height)`:

```rust
fn load_hashchain<I: IO>(io: &I, block_height: u64) -> Result<Option<Hashchain>, ContractError> {
    let mut maybe_hashchain = read_current_hashchain(io)?;
    if let Some(hashchain) = maybe_hashchain.as_mut()
        && block_height > hashchain.get_current_block_height()
    {
        hashchain.move_to_block(block_height)?;   // <-- unbounded
    }
    Ok(maybe_hashchain)
}
```

`move_to_block` in `engine-hashchain/src/hashchain.rs:67-88` contains an unbounded while loop that iterates once per skipped block, computing a keccak hash on each iteration:

```rust
pub fn move_to_block(&mut self, next_block_height: u64) -> Result<(), BlockchainHashchainError> {
    // ...
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

`compute_block_hashchain` (`engine-hashchain/src/hashchain.rs:269-289`) calls `keccak` once per iteration plus `txs_merkle_tree.compute_hash()`. Each iteration is non-trivial in gas cost.

`resume_contract` (`engine/src/contract_methods/admin.rs:263-272`) is itself wrapped in `with_hashchain`:

```rust
pub fn resume_contract<I: IO + Copy, E: Env>(io: I, env: &E) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        // ...
        state.is_paused = false;
        state::set_state(&mut io, &state)?;
        Ok(())
    })
}
```

This means `move_to_block` is called **before** the paused flag is cleared. If the engine was paused for N blocks, `move_to_block` must iterate N times. NEAR has a hard per-transaction gas limit of 300 Tgas. Once N is large enough that the loop exhausts this budget, `resume_contract` panics and the contract remains permanently paused.

NEAR produces approximately one block per second. A pause of even a few hours (thousands of blocks) may be sufficient to exhaust gas, depending on the exact per-iteration cost of `compute_block_hashchain` and `compute_hash` in the Merkle tree.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

All ETH and ERC-20 tokens held by users on Aurora are custodied by the Aurora Engine contract. When the engine is paused, users cannot withdraw via `ExitToNear` or `ExitToEthereum` (both require `require_running`). If `resume_contract` is also permanently broken due to gas exhaustion, the pause becomes irreversible. Every user's bridged ETH and token balances are permanently inaccessible with no recovery path available through the contract's own interface.

---

### Likelihood Explanation

**Low.** The scenario requires:
1. The hashchain feature is enabled (set via `initial_hashchain` in `NewCallArgsV4`).
2. The admin legitimately pauses the contract (e.g., for an emergency or upgrade).
3. The pause lasts long enough for the block gap to exhaust NEAR's 300 Tgas limit.

No attacker action is required beyond waiting. The admin's own pause action, combined with elapsed time, triggers the freeze. This mirrors the external report's likelihood profile exactly.

---

### Recommendation

Replace the unbounded while loop in `move_to_block` with a design that does not require iterating every skipped block. Options include:

1. **Skip empty blocks**: Instead of hashing each empty block individually, compute a single aggregate hash for a range of empty blocks (e.g., hash the range `[start_height, end_height)` in one keccak call).
2. **Lazy catch-up**: Store only the last block height and hashchain value; when catching up, compute a single "gap hash" that commits to the range without iterating it.
3. **Remove `move_to_block` from `resume_contract`**: Exempt `resume_contract` (and `pause_contract`) from the hashchain wrapping, or handle the catch-up lazily on the first post-resume transaction with a bounded iteration cap.

---

### Proof of Concept

**Step 1**: Deploy Aurora Engine with hashchain enabled (`initial_hashchain` set in `NewCallArgsV4`).

**Step 2**: Admin calls `pause_contract`. The hashchain is saved at block height B.

**Step 3**: Wait for N NEAR blocks to pass (N large enough to exhaust 300 Tgas in `move_to_block`'s loop). NEAR produces ~1 block/second, so N ≈ hours to days depending on per-iteration gas cost.

**Step 4**: Admin calls `resume_contract`. Execution path:

```
resume_contract
  └─ with_hashchain (engine/src/hashchain.rs:21)
       └─ load_hashchain (engine/src/hashchain.rs:88)
            └─ hashchain.move_to_block(B + N)  (engine-hashchain/src/hashchain.rs:75)
                 └─ while loop: N iterations of keccak → GAS EXHAUSTION → PANIC
```

**Step 5**: `resume_contract` reverts. The engine remains paused. Repeat Step 4 indefinitely — it always fails. All user funds are permanently frozen.

**Relevant code locations**:

- Unbounded loop: [1](#0-0) 
- `load_hashchain` calling `move_to_block`: [2](#0-1) 
- `resume_contract` wrapped in `with_hashchain`: [3](#0-2) 
- `with_hashchain` calling `load_hashchain`: [4](#0-3) 
- `compute_block_hashchain` (keccak per iteration): [5](#0-4)

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

**File:** engine/src/hashchain.rs (L32-34)
```rust
    let block_height = env.block_height();
    let maybe_hashchain = load_hashchain(&io, block_height)?;

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

**File:** engine/src/contract_methods/admin.rs (L263-272)
```rust
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
