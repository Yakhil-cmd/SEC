### Title
Deterministic `BLOCKHASH` Opcode Implementation Enables Precomputable Randomness Exploitation — (`engine/src/engine.rs`)

---

### Summary

Aurora Engine's `block_hash()` implementation computes the EVM `BLOCKHASH` opcode result as a pure function of fully public, deterministic inputs (`chain_id`, `account_id`, `block_height`). This means every past and future Aurora block hash is precomputable by any observer before a transaction is submitted. Any EVM contract deployed on Aurora that uses `blockhash` for randomness — a widespread Solidity pattern — is trivially exploitable for fund theft.

---

### Finding Description

The `compute_block_hash` function, which backs the EVM `BLOCKHASH` opcode for all contracts running on Aurora, is defined as:

```
sha256(BLOCK_HASH_PREFIX || chain_id || account_id || block_height)
``` [1](#0-0) 

All four inputs are constants or publicly known values that are fixed before any transaction executes. `chain_id` and `account_id` are static contract parameters; `block_height` is the NEAR block number, which is public and monotonically increasing. There is no unpredictable entropy — no VRF output, no transaction data, no state root — mixed into the formula.

The `block_hash()` method in the `Backend` implementation returns this deterministic value for any block within the last 256 blocks: [2](#0-1) 

This is structurally analogous to the external report's `REQUEST_CONFIRMATIONS = 3` being too low: in that case, the VRF seed is insufficiently finalized and can change due to reorgs; here, the "block hash" entropy is **zero** — it never contained any unpredictable component to begin with. Both root causes produce the same class of impact: a randomness source that an attacker can know or control before committing their transaction.

Contrast this with `block_randomness()`, which correctly uses NEAR's per-block VRF output for the `PREVRANDAO` opcode: [3](#0-2) 

The same VRF entropy is also exposed via the Aurora-specific `RandomSeed` precompile at `0xc104f4840573bed437190daf5d2898c2bdf928ac`: [4](#0-3) 

`PREVRANDAO` and the `RandomSeed` precompile are secure. `BLOCKHASH` is not. The engine itself acknowledges this is a deviation from standard EVM behavior and links to an open nearcore issue, but the deviation is not merely a compatibility note — it is an exploitable security property.

---

### Impact Explanation

**Critical — Direct theft of user funds.**

Any EVM contract on Aurora that uses `blockhash(block.number - 1)` (or any recent block number) as a randomness source is fully exploitable. An attacker can:

1. Precompute `sha256(BLOCK_HASH_PREFIX || chain_id || account_id || N)` for any target block `N` before submitting a transaction.
2. Determine the outcome of the randomness-dependent contract call (lottery winner, NFT trait, game result) before committing.
3. Submit only when the outcome is favorable, or withhold submission otherwise.

This enables guaranteed wins in any on-chain game, lottery, or randomness-gated distribution deployed on Aurora that relies on `blockhash`. Funds locked in such contracts are directly stealable.

---

### Likelihood Explanation

**High.**

`blockhash(block.number - 1)` is one of the most common randomness patterns in Solidity, predating `PREVRANDAO`. Aurora markets itself as EVM-compatible, so developers porting contracts from Ethereum or writing new contracts expect `blockhash` to carry at least some unpredictability (as it does on Ethereum, where it commits to the full block header including transactions and state). On Aurora it carries none. The mismatch between developer expectation and actual behavior makes exploitation likely wherever such contracts exist.

---

### Recommendation

Mix NEAR's per-block VRF random seed (`env.random_seed()`) into the `compute_block_hash` formula so that `BLOCKHASH` values are not precomputable:

```
block_hash = sha256(BLOCK_HASH_PREFIX || chain_id || account_id || block_height || near_random_seed_at(block_height))
```

This aligns `BLOCKHASH` with the same entropy source already used correctly by `block_randomness()` / `PREVRANDAO`. Alternatively, clearly gate any randomness-sensitive contract deployment on Aurora to use the `RandomSeed` precompile or `PREVRANDAO` exclusively, and document `BLOCKHASH` as cryptographically unsafe for randomness on Aurora.

---

### Proof of Concept

```
Given:
  BLOCK_HASH_PREFIX = 0x00  (constant in engine/src/engine.rs)
  chain_id          = [0u8; 32] padded Aurora chain ID (e.g. 1313161554)
  account_id        = b"aurora"
  target_block      = current_block_height - 1  (publicly observable)

Attacker precomputes:
  predicted_hash = sha256(BLOCK_HASH_PREFIX || chain_id || account_id || target_block.to_be_bytes())

Attacker calls a lottery contract that does:
  uint256 rand = uint256(blockhash(block.number - 1)) % 100;
  if (rand == 42) { winner.transfer(prize); }

Attacker evaluates predicted_hash % 100 off-chain.
If == 42, submits the transaction and collects the prize.
If != 42, does not submit (no cost, no loss).
Repeat each block until winning condition is met.
```

The test file `engine-tests/src/tests/res/blockhash.sol` confirms that Aurora block hashes are stable, deterministic constants verifiable at compile time: [5](#0-4)

### Citations

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

**File:** engine/src/engine.rs (L1805-1817)
```rust
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

**File:** engine-precompiles/src/random.rs (L19-32)
```rust
impl RandomSeed {
    /// Random bytes precompile address
    /// This is a per-block entropy source which could then be used to create a random sequence.
    /// It will return the same seed if called multiple time in the same block.
    ///
    /// Address: `0xc104f4840573bed437190daf5d2898c2bdf928ac`
    /// This address is computed as: `&keccak("randomSeed")[12..]`
    pub const ADDRESS: Address = make_address(0xc104f484, 0x0573bed437190daf5d2898c2bdf928ac);

    #[must_use]
    pub const fn new(random_seed: H256) -> Self {
        Self { random_seed }
    }
}
```

**File:** engine-tests/src/tests/res/blockhash.sol (L1-13)
```text
// SPDX-License-Identifier: Unlicense
pragma solidity ^0.8.0;

contract BlockHash {
  constructor() payable {}

  function test() public view {
    require(
      blockhash(0) == hex"ec035c7409243a343a8fd798077fb0a5f879cc32c9cd31fd07baa2292e4d3d7c",
      "Bad block hash"
    );
  }
}
```
