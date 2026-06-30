### Title
EVM User Can Bias `PREVRANDAO` / `RandomSeed` via Calldata Crafting — (File: `engine/src/engine.rs`, `engine-precompiles/src/random.rs`)

---

### Summary

Aurora Engine exposes the NEAR per-receipt `random_seed` as the EVM `PREVRANDAO` value and via the Aurora-specific `RandomSeed` precompile. Because the NEAR per-receipt random seed is derived from the `action_hash` — which is a function of the user's submitted calldata — any unprivileged EVM user can bias the `random_seed` seen inside the EVM by iterating over crafted calldata variants. EVM contracts that rely on `PREVRANDAO` or the `RandomSeed` precompile as a source of randomness for fund distribution are directly exploitable.

---

### Finding Description

**Root cause — `block_randomness()` returns a per-receipt, calldata-dependent value:**

`block_randomness()` in `engine/src/engine.rs` returns `Some(self.env.random_seed())`: [1](#0-0) 

In the production NEAR runtime, `random_seed()` is the NEAR host function `exports::random_seed`: [2](#0-1) 

In NEAR Protocol the per-receipt random seed is derived from the block-level VRF output **and** the receipt ID. The receipt ID is a function of the NEAR transaction hash, which includes the user's input bytes (the RLP-encoded Ethereum transaction / calldata). The standalone-storage sync layer makes this dependency explicit: [3](#0-2) 

`action_hash` is per-transaction and changes with every byte of calldata. Therefore the `random_seed` delivered to the EVM is **not** a fixed per-block value — it is a per-receipt value that the submitting user can influence by varying their calldata.

**Exposure surface — `PREVRANDAO` opcode and `RandomSeed` precompile:**

The biased `random_seed` is returned to EVM contracts through two paths:

1. The `PREVRANDAO` opcode (post-merge EVM semantics), which calls `block_randomness()` above.
2. The Aurora-specific `RandomSeed` precompile at `0xc104f4840573bed437190daf5d2898c2bdf928ac`, which returns `self.random_seed.as_bytes()` directly: [4](#0-3) 

The precompile comment claims "It will return the same seed if called multiple times in the same block," which is true within one receipt, but misleads developers into believing the value is block-scoped and therefore as hard to bias as Ethereum's `PREVRANDAO`. In reality it is receipt-scoped and fully user-influenceable.

**Attack mechanics (analogous to the Taiko `blobHash` XOR bias):**

Just as the Taiko proposer appends a no-op transaction and increments its calldata byte-by-byte until `meta.difficulty` meets a threshold, an Aurora user:

1. Learns the block-level NEAR VRF output (predictable once the epoch boundary is known, exactly as `block.prevrandao` is predictable in the Taiko report).
2. Appends an arbitrary suffix (e.g., `0x01`, `0x02`, …) to their Ethereum transaction's calldata. The suffix is ignored by the target contract but changes the NEAR `action_hash`, hence the `random_seed`.
3. Computes the expected `random_seed` for each candidate calldata offline.
4. Submits the variant whose `random_seed` satisfies the desired condition (e.g., `random_seed % N == attacker_index`).

The `SubmitArgs` struct accepted by `submit_with_args` makes this even more direct — the user controls `tx_data` byte-for-byte: [5](#0-4) 

---

### Impact Explanation

Any EVM contract deployed on Aurora that uses `block.prevrandao` or calls the `RandomSeed` precompile as a source of randomness for fund distribution (lotteries, random NFT mints, random reward selection, etc.) is exploitable. The attacker can deterministically select the outcome before submitting the transaction, constituting **direct theft of user funds** held in those contracts.

**Impact: Critical** — direct theft of user funds at rest in randomness-dependent contracts.

---

### Likelihood Explanation

- The block-level NEAR VRF output is predictable from the epoch boundary (same caveat noted in the Taiko report for `block.prevrandao`).
- Iterating calldata suffixes is cheap and fully off-chain; no special privilege is required.
- The `RandomSeed` precompile is Aurora-specific and marketed as an entropy source, making it an attractive primitive for contract developers who may not understand its per-receipt, calldata-dependent nature.
- Any deployed contract using `PREVRANDAO` or the precompile for randomness is an immediate target.

**Likelihood: High** — no privilege required; attack is fully off-chain computation followed by a single on-chain transaction.

---

### Recommendation

1. **Document the per-receipt nature explicitly.** The precompile comment currently says "per-block entropy source," which is incorrect and misleading. It should state that the seed is per-receipt and is influenced by the submitter's calldata.
2. **Remove calldata from the seed derivation**, or mix in an additional unpredictable commitment (e.g., a future block hash) so that the user cannot precompute the seed before the block is finalized.
3. **Warn contract developers** that `PREVRANDAO` on Aurora does not have the same security properties as on Ethereum mainnet — individual users, not just block proposers, can bias it.
4. **Consider a VRF oracle or commit-reveal scheme** for any on-chain randomness that governs fund distribution.

---

### Proof of Concept

```solidity
// Lottery.sol deployed on Aurora
contract Lottery {
    address[] public participants;
    function enter() external payable { participants.push(msg.sender); }
    function draw() external {
        // Uses PREVRANDAO — attacker-biasable on Aurora
        uint winner = uint(block.prevrandao) % participants.length;
        payable(participants[winner]).transfer(address(this).balance);
    }
}
```

**Attacker steps:**

1. Wait until the NEAR epoch boundary; derive the block-level VRF output `V`.
2. For suffix `s = 0, 1, 2, …`:
   - Construct Ethereum tx with calldata `enter()` + `s` as trailing bytes.
   - Compute `action_hash = keccak(near_tx_with_suffix_s)`.
   - Compute `expected_seed = compute_random_seed(action_hash, V)`.
   - If `uint(expected_seed) % participants.length == attacker_index`, stop.
3. Submit the Ethereum transaction with suffix `s` via `submit` or `submit_with_args`.
4. Call `draw()` in the same or next block — `block.prevrandao` equals `expected_seed`; attacker wins.

The entry path is the standard `submit` / `submit_with_args` NEAR method, reachable by any unprivileged EVM user: [6](#0-5)

### Citations

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

**File:** engine-standalone-storage/src/sync/mod.rs (L407-410)
```rust
    let random_seed = compute_random_seed(
        &transaction_message.action_hash,
        &block_metadata.random_seed,
    );
```

**File:** engine-precompiles/src/random.rs (L15-58)
```rust
pub struct RandomSeed {
    random_seed: H256,
}

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

impl Precompile for RandomSeed {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::RANDOM_BYTES_GAS)
    }

    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        context: &Context,
        _is_static: bool,
    ) -> EvmPrecompileResult {
        utils::validate_no_value_attached_to_precompile(context.apparent_value)?;
        let cost = Self::required_gas(input)?;
        if let Some(target_gas) = target_gas
            && cost > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        Ok(PrecompileOutput::without_logs(
            cost,
            self.random_seed.as_bytes().to_vec(),
        ))
    }
```

**File:** engine-types/src/parameters/engine.rs (L132-140)
```rust
#[derive(Default, Debug, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
pub struct SubmitArgs {
    /// Bytes of the transaction.
    pub tx_data: Vec<u8>,
    /// Max gas price the user is ready to pay for the transaction.
    pub max_gas_price: Option<u128>,
    /// Address of the `ERC20` token the user prefers to pay in.
    pub gas_token_address: Option<Address>,
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
