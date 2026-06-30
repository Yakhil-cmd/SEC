### Title
Fully Predictable `blockhash` Opcode Values Enable Attacker-Controlled Exploitation of Any EVM Contract Using `blockhash` as an Entropy or Oracle Source — (File: `engine/src/engine.rs`)

---

### Summary

Aurora Engine's `Backend::block_hash` implementation computes a fully deterministic, pre-computable SHA-256 digest from entirely public inputs: a fixed prefix byte, the chain ID, the engine account ID, and the block height. Every future `blockhash(N)` value is therefore known to any observer before block `N` is produced. Any EVM contract deployed on Aurora that consumes `blockhash` as a source of randomness, a price-oracle seed, or a TWAP anchor is trivially manipulable by an unprivileged attacker.

---

### Finding Description

**Root cause — `compute_block_hash` in `engine/src/engine.rs`:**

```rust
// engine/src/engine.rs  lines 1239-1252
pub fn compute_block_hash(chain_id: [u8; 32], block_height: u64, account_id: &[u8]) -> H256 {
    let mut data = Vec::with_capacity(
        BLOCK_HASH_PREFIX_SIZE + BLOCK_HEIGHT_SIZE + CHAIN_ID_SIZE + account_id.len(),
    );
    data.push(BLOCK_HASH_PREFIX);          // constant 0x00
    data.extend_from_slice(&chain_id);     // public, fixed per deployment
    data.extend_from_slice(account_id);    // public, fixed per deployment
    data.extend_from_slice(&block_height.to_be_bytes()); // public, known in advance
    sdk::sha256(&data)
}
```

All four inputs are public constants or monotonically increasing public counters. The SHA-256 output is therefore a pure function of the block number, computable by anyone at any time.

**Call site — `Backend::block_hash` in `engine/src/engine.rs`:**

```rust
// engine/src/engine.rs  lines 1805-1817
fn block_hash(&self, number: U256) -> H256 {
    let idx = U256::from(self.env.block_height());
    if idx.saturating_sub(U256::from(256)) <= number && number < idx {
        compute_block_hash(
            self.state.chain_id,
            number.low_u64(),
            self.current_account_id.as_bytes(),
        )
    } else {
        H256::zero()
    }
}
```

This is the value the EVM exposes to every Solidity contract via the `BLOCKHASH` opcode. The stale-data analogy to the external report is exact: just as the Chainlink BAYC feed returned `68.6 ETH` for a full day without updating, Aurora's `blockhash` never carries any unpredictable entropy — it is structurally equivalent to a price feed that always returns a value computed solely from already-public parameters.

**Compounding factor — stale comment contradicts actual behavior:**

The doc-comment immediately above the call site states the function "returns `0xfff…fff` for the 256 most recent blocks." The actual code returns a SHA-256 hash. Developers reading the comment believe the value is a sentinel constant; they do not realise it is a deterministic oracle they can query in advance. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

**Classification:** Critical — direct theft of user funds.

Any EVM contract on Aurora that uses `blockhash` as an entropy source (lotteries, NFT fair-mints, commit-reveal schemes, on-chain TWAP seeds) is fully broken. An attacker can:

1. Pre-compute `sha256(0x00 ‖ chain_id ‖ account_id ‖ target_block_height)` off-chain before the target block is produced.
2. Determine the winning outcome of the victim contract before submitting any transaction.
3. Submit a single transaction in the target block that claims the winning outcome, draining the contract's funds.

Because NEAR block production is ~1 second and the hash is computable in microseconds, the attacker has ample time to act. No privileged access is required; any EOA can call `submit()` on the Aurora engine. [3](#0-2) 

---

### Likelihood Explanation

**High.** The `BLOCKHASH` opcode is a standard Ethereum primitive. Solidity developers routinely use it for cheap on-chain randomness (a pattern already discouraged on mainnet Ethereum but still widely deployed). Aurora's EVM compatibility guarantee leads developers to assume `blockhash` behaves as on Ethereum — i.e., that it is unpredictable before the block is sealed. The misleading doc-comment reinforces this false assumption. Any such contract deployed on Aurora is immediately exploitable by any observer who reads `compute_block_hash`. [1](#0-0) [4](#0-3) 

---

### Recommendation

1. **Correct the doc-comment** on `block_hash` to accurately describe the SHA-256 construction and explicitly warn that the value is pre-computable.
2. **Surface a protocol-level warning** in the `submit` / `view` entrypoints (or via a dedicated precompile) that `BLOCKHASH` on Aurora is deterministic and must not be used as an entropy source.
3. **Provide a secure randomness precompile** backed by NEAR's VRF (`env::random_seed()`), which is already available via `self.env.random_seed()` in the `Backend` implementation, and document it as the correct replacement for `blockhash`-based randomness.
4. **Consider returning `H256::zero()` for all blocks** (matching the documented sentinel) until a cryptographically unpredictable source is available, so that contracts that check `blockhash != 0` as a liveness guard fail safely rather than silently accepting a manipulable value. [5](#0-4) [6](#0-5) 

---

### Proof of Concept

```python
# Off-chain pre-computation (Python pseudocode)
import hashlib, struct

BLOCK_HASH_PREFIX = b'\x00'
CHAIN_ID = bytes.fromhex("4e454152")  # Aurora mainnet chain_id (padded to 32 bytes)
ACCOUNT_ID = b"aurora"               # engine account id
TARGET_BLOCK = 130_000_000           # future NEAR block height

data = (BLOCK_HASH_PREFIX
        + CHAIN_ID.rjust(32, b'\x00')
        + ACCOUNT_ID
        + struct.pack(">Q", TARGET_BLOCK))

predicted_hash = hashlib.sha256(data).hexdigest()
print(f"blockhash({TARGET_BLOCK}) = 0x{predicted_hash}")
```

**Attack flow:**

1. Attacker identifies a lottery contract on Aurora that resolves via `uint256 winner = uint256(blockhash(block.number - 1)) % totalTickets`.
2. Attacker runs the script above for the next NEAR block height to obtain `predicted_hash`.
3. Attacker computes `winner_index = uint256(predicted_hash) % totalTickets`.
4. Attacker purchases only ticket `winner_index` in the same NEAR block.
5. Attacker calls `claimPrize()` and drains the prize pool.

No special privileges, no flash loans, no governance — a single `submit()` call to the Aurora engine suffices. [1](#0-0) [3](#0-2)

### Citations

**File:** engine/src/engine.rs (L51-57)
```rust
/// Used as the first byte in the concatenation of data used to compute the blockhash.
/// Could be useful in the future as a version byte, or to distinguish different types of blocks.
const BLOCK_HASH_PREFIX: u8 = 0;
const BLOCK_HASH_PREFIX_SIZE: usize = 1;
const BLOCK_HEIGHT_SIZE: usize = 8;
const CHAIN_ID_SIZE: usize = 32;

```

**File:** engine/src/engine.rs (L1239-1252)
```rust
pub fn compute_block_hash(chain_id: [u8; 32], block_height: u64, account_id: &[u8]) -> H256 {
    debug_assert_eq!(BLOCK_HASH_PREFIX_SIZE, size_of_val(&BLOCK_HASH_PREFIX));
    debug_assert_eq!(BLOCK_HEIGHT_SIZE, size_of_val(&block_height));
    debug_assert_eq!(CHAIN_ID_SIZE, size_of_val(&chain_id));
    let mut data = Vec::with_capacity(
        BLOCK_HASH_PREFIX_SIZE + BLOCK_HEIGHT_SIZE + CHAIN_ID_SIZE + account_id.len(),
    );
    data.push(BLOCK_HASH_PREFIX);
    data.extend_from_slice(&chain_id);
    data.extend_from_slice(account_id);
    data.extend_from_slice(&block_height.to_be_bytes());

    sdk::sha256(&data)
}
```

**File:** engine/src/engine.rs (L1788-1817)
```rust
    /// Returns a block hash from a given index.
    ///
    /// Currently, this returns
    /// 0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff if
    /// only for the 256 most recent blocks, excluding of the current one.
    /// Otherwise, it returns 0x0.
    ///
    /// In other words, if the requested block index is less than the current
    /// block index, return
    /// 0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff.
    /// Otherwise, return 0.
    ///
    /// This functionality may change in the future. Follow
    /// [nearcore#3456](https://github.com/near/nearcore/issues/3456) for more
    /// details.
    ///
    /// See: `https://doc.aurora.dev/develop/compat/evm#blockhash`
    fn block_hash(&self, number: U256) -> H256 {
        let idx = U256::from(self.env.block_height());
        if idx.saturating_sub(U256::from(256)) <= number && number < idx {
            // since `idx` comes from `u64` it is always safe to downcast `number` from `U256`
            compute_block_hash(
                self.state.chain_id,
                number.low_u64(),
                self.current_account_id.as_bytes(),
            )
        } else {
            H256::zero()
        }
    }
```

**File:** engine/src/engine.rs (L1847-1850)
```rust
    /// Get environmental block randomness.
    fn block_randomness(&self) -> Option<H256> {
        Some(self.env.random_seed())
    }
```

**File:** engine-sdk/src/near_runtime.rs (L384-391)
```rust
    fn random_seed(&self) -> H256 {
        unsafe {
            exports::random_seed(0);
            let mut bytes = H256::zero();
            exports::read_register(0, bytes.0.as_mut_ptr() as u64);
            bytes
        }
    }
```
