Looking at the full execution path carefully before rendering a verdict.

### Title
ETH Permanently Locked at Standard Precompile Addresses Due to Missing Value Validation — (`engine-precompiles/src/hash.rs`)

---

### Summary

`SHA256::run` and `RIPEMD160::run` (and all other standard Ethereum precompiles: `ECRecover`, `Identity`, `ModExp`, `Bn256Add/Mul/Pair`, `Blake2F`, BLS12-381 suite) do not call `utils::validate_no_value_attached_to_precompile`. Any EVM user can send a CALL with `value > 0` to these precompile addresses. The SputnikVM executor transfers the value to the precompile address before invoking the precompile logic. The precompile succeeds and returns its output. The ETH is then permanently credited to the precompile address, which has no private key and no deployed code, making it irrecoverable. This grows the gap between total bridged ETH (NEP-141 supply) and total withdrawable ETH with each such call.

---

### Finding Description

**Guard coverage is asymmetric.** `validate_no_value_attached_to_precompile` is called in exactly six places: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

None of the standard Ethereum precompiles have this guard. `SHA256::run` and `RIPEMD160::run` ignore `context.apparent_value` entirely: [7](#0-6) [8](#0-7) 

The same is true for `ECRecover`, `Identity`, `Blake2F`, and all BN256/BLS precompiles. [9](#0-8) [10](#0-9) [11](#0-10) 

**Value transfer mechanics.** `Engine::call` passes `value.raw()` directly to `executor.transact_call`: [12](#0-11) 

The SputnikVM `StackExecutor::transact_call` debits the caller and credits the callee (the precompile address) before dispatching to the precompile's `execute` method. This is standard EVM Yellow Paper behavior. The `context.apparent_value` field passed to `run` is the evidence that the executor has already committed the value transfer.

**Accounting is blind to this.** `ApplyBackend::apply` computes a net change across all accounts. Sender loses X, precompile address gains X → `accounting.net()` returns `Net::Zero`, which is explicitly allowed: [13](#0-12) 

No invariant is tripped. The ETH is silently credited to the precompile address.

**No recovery path exists.** Precompile addresses `0x0000...0001` through `0x0000...0009` (and the BLS addresses) have no known private key and no deployed code. The only withdrawal mechanisms in Aurora are `ExitToNear` and `ExitToEthereum`, both of which require the caller to initiate a transaction from the address holding the ETH. Neither can be invoked from a keyless, codeless address. [14](#0-13) [15](#0-14) 

---

### Impact Explanation

Aurora's ETH is bridged: every wei in the EVM corresponds to a real ETH (or equivalent) locked in the eth-connector, tracked by the NEP-141 supply. When ETH is sent to a precompile address:

- The NEP-141 supply is **unchanged** (no withdrawal occurred).
- The total EVM balance sum is **unchanged** (just redistributed).
- The ETH at the precompile address **can never be withdrawn**, so total withdrawable ETH decreases.

Result: `total_bridged_ETH (NEP-141 supply) > total_withdrawable_ETH`, growing monotonically with each such call. This is permanent freezing of funds and satisfies the insolvency definition in the scope.

---

### Likelihood Explanation

The attack requires only a funded EVM account and a standard EVM CALL with `value > 0` to address `0x0000000000000000000000000000000000000002`. No special privileges, no contract deployment, no admin access. Any user who accidentally or intentionally sends value to a precompile address triggers this. The cost to the attacker is only the ETH they choose to lock (plus gas).

---

### Recommendation

Add `utils::validate_no_value_attached_to_precompile(context.apparent_value)?;` as the first statement in `run` for every standard precompile that does not legitimately consume value: `SHA256`, `RIPEMD160`, `ECRecover`, `Identity`, `ModExp`, `Bn256Add`, `Bn256Mul`, `Bn256Pair`, `Blake2F`, and all BLS12-381 precompiles. [16](#0-15) 

This is already the established pattern for all Aurora-specific precompiles and costs nothing at runtime beyond a single comparison.

---

### Proof of Concept

**Unit-level (no infra needed):**

```rust
// engine-precompiles/src/hash.rs (in #[cfg(test)] mod tests)
#[test]
fn test_sha256_accepts_nonzero_value() {
    use aurora_engine_types::U256;
    let mut ctx = new_context();
    ctx.apparent_value = U256::from(1_000_000_000_000_000_000u64); // 1 ETH
    // This succeeds — no value guard exists
    let result = SHA256.run(b"hello", Some(EthGas::new(100)), &ctx, false);
    assert!(result.is_ok(), "SHA256 should have rejected nonzero value but did not");
}
```

**Integration-level (using the existing `AuroraRunner` harness):**

```rust
// Pseudocode using engine-tests infrastructure
let sha256_address = SHA256::ADDRESS; // 0x0000...0002
let value = Wei::new_u64(1_000); // 1000 wei

// Fund attacker
runner.mint(attacker, Wei::new_u64(100_000));

// Record pre-state
let pre_balance = runner.get_balance(sha256_address);

// Submit CALL to SHA256 with value
let tx = TransactionLegacy {
    to: Some(sha256_address),
    value,
    data: b"any input".to_vec(),
    ..
};
let result = runner.submit_transaction(&attacker_key, tx).unwrap();
assert!(result.status.is_ok()); // succeeds — hash is returned

// Post-state: SHA256 address now holds ETH
let post_balance = runner.get_balance(sha256_address);
assert_eq!(post_balance, pre_balance + value); // ETH credited

// No withdrawal path: ExitToNear/ExitToEthereum cannot be called from 0x0000...0002
// ETH is permanently locked
```

The precompile succeeds, the hash output is returned to the caller, and the value is permanently credited to `SHA256::ADDRESS` with no recovery mechanism. [17](#0-16) [16](#0-15) [18](#0-17) [19](#0-18)

### Citations

**File:** engine-precompiles/src/account_ids.rs (L51-51)
```rust
        utils::validate_no_value_attached_to_precompile(context.apparent_value)?;
```

**File:** engine-precompiles/src/account_ids.rs (L96-96)
```rust
        utils::validate_no_value_attached_to_precompile(context.apparent_value)?;
```

**File:** engine-precompiles/src/prepaid_gas.rs (L44-44)
```rust
        utils::validate_no_value_attached_to_precompile(context.apparent_value)?;
```

**File:** engine-precompiles/src/random.rs (L46-46)
```rust
        utils::validate_no_value_attached_to_precompile(context.apparent_value)?;
```

**File:** engine-precompiles/src/promise_result.rs (L48-48)
```rust
        utils::validate_no_value_attached_to_precompile(context.apparent_value)?;
```

**File:** engine-precompiles/src/xcc.rs (L108-108)
```rust
        utils::validate_no_value_attached_to_precompile(context.apparent_value)?;
```

**File:** engine-precompiles/src/hash.rs (L29-61)
```rust
impl SHA256 {
    pub const ADDRESS: Address = make_address(0, 2);
}

impl Precompile for SHA256 {
    fn required_gas(input: &[u8]) -> Result<EthGas, ExitError> {
        let input_len = u64::try_from(input.len()).map_err(utils::err_usize_conv)?;
        Ok(
            input_len.div_ceil(consts::SHA256_WORD_LEN) * costs::SHA256_PER_WORD
                + costs::SHA256_BASE,
        )
    }

    /// See: `https://ethereum.github.io/yellowpaper/paper.pdf`
    /// See: `https://docs.soliditylang.org/en/develop/units-and-global-variables.html#mathematical-and-cryptographic-functions`
    /// See: `https://etherscan.io/address/0000000000000000000000000000000000000002`
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        _context: &Context,
        _is_static: bool,
    ) -> EvmPrecompileResult {
        let cost = Self::required_gas(input)?;
        if let Some(target_gas) = target_gas
            && cost > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        let output = sdk::sha256(input).as_bytes().to_vec();
        Ok(PrecompileOutput::without_logs(cost, output))
    }
```

**File:** engine-precompiles/src/hash.rs (L83-103)
```rust
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        _context: &Context,
        _is_static: bool,
    ) -> EvmPrecompileResult {
        let cost = Self::required_gas(input)?;
        if let Some(target_gas) = target_gas
            && cost > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        let hash = sdk::ripemd160(input);
        // The result needs to be padded with leading zeros because it is only 20 bytes, but
        // the evm works with 32-byte words.
        let mut output = vec![0u8; 32];
        output[12..].copy_from_slice(&hash);
        Ok(PrecompileOutput::without_logs(cost, output))
    }
```

**File:** engine-precompiles/src/secp256k1.rs (L41-53)
```rust
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        _context: &Context,
        _is_static: bool,
    ) -> EvmPrecompileResult {
        let cost = Self::required_gas(input)?;
        if let Some(target_gas) = target_gas
            && cost > target_gas
        {
            return Err(ExitError::OutOfGas);
        }
```

**File:** engine-precompiles/src/identity.rs (L41-56)
```rust
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        _context: &Context,
        _is_static: bool,
    ) -> EvmPrecompileResult {
        let cost = Self::required_gas(input)?;
        if let Some(target_gas) = target_gas
            && cost > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        Ok(PrecompileOutput::without_logs(cost, input.to_vec()))
    }
```

**File:** engine-precompiles/src/blake2.rs (L162-179)
```rust
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        _context: &Context,
        _is_static: bool,
    ) -> EvmPrecompileResult {
        if input.len() != consts::INPUT_LENGTH {
            return Err(ExitError::Other(Borrowed("ERR_BLAKE2F_INVALID_LEN")));
        }

        let cost = Self::required_gas(input)?;
        if let Some(target_gas) = target_gas
            && cost > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

```

**File:** engine/src/engine.rs (L640-648)
```rust
        let (exit_reason, result) = executor.transact_call(
            origin.raw(),
            contract.raw(),
            value.raw(),
            input,
            gas_limit,
            access_list,
            authorization_list,
        );
```

**File:** engine/src/engine.rs (L1974-2059)
```rust
impl<J: IO + Copy, E: Env, M: ModExpAlgorithm> ApplyBackend for Engine<'_, J, E, M> {
    fn apply<A, I, L>(&mut self, values: A, _logs: L, delete_empty: bool)
    where
        A: IntoIterator<Item = Apply<I>>,
        I: IntoIterator<Item = (H256, H256)>,
        L: IntoIterator<Item = Log>,
    {
        let mut writes_counter: usize = 0;
        let mut code_bytes_written: usize = 0;
        let mut accounting = accounting::Accounting::default();
        for apply in values {
            match apply {
                Apply::Modify {
                    address,
                    basic,
                    code,
                    storage,
                    reset_storage,
                } => {
                    let current_basic = self.basic(address);
                    accounting.change(accounting::Change {
                        new_value: basic.balance,
                        old_value: current_basic.balance,
                    });

                    let address = Address::new(address);
                    let generation = get_generation(&self.io, &address);

                    if current_basic.nonce != basic.nonce {
                        set_nonce(&mut self.io, &address, &basic.nonce);
                        writes_counter += 1;
                    }
                    if current_basic.balance != basic.balance {
                        set_balance(&mut self.io, &address, &Wei::new(basic.balance));
                        writes_counter += 1;
                    }

                    if let Some(code) = code {
                        set_code(&mut self.io, &address, &code);
                        code_bytes_written = code.len();
                        sdk::log!("code_write_at_address {:?} {}", address, code_bytes_written);
                    }

                    let next_generation = if reset_storage {
                        remove_all_storage(&mut self.io, &address, generation);
                        generation + 1
                    } else {
                        generation
                    };

                    for (index, value) in storage {
                        if value == H256::default() {
                            remove_storage(&mut self.io, &address, &index, next_generation);
                        } else {
                            set_storage(&mut self.io, &address, &index, &value, next_generation);
                        }
                        writes_counter += 1;
                    }

                    // We only need to remove the account if:
                    // 1. we are supposed to delete an empty account
                    // 2. the account is empty
                    // 3. we didn't already clear out the storage (because if we did then there is
                    //    nothing to do)
                    if delete_empty
                        && is_account_empty(&self.io, &address)
                        && generation == next_generation
                    {
                        remove_account(&mut self.io, &address, generation);
                        writes_counter += 1;
                    }
                }
                Apply::Delete { address } => {
                    let current_basic = self.basic(address);
                    accounting.remove(current_basic.balance);

                    let address = Address::new(address);
                    let generation = get_generation(&self.io, &address);
                    remove_account(&mut self.io, &address, generation);
                    writes_counter += 1;
                }
            }
        }
        match accounting.net() {
            // Net loss is possible if `SELFDESTRUCT(self)` calls are made.
            accounting::Net::Zero | accounting::Net::Lost(_) => (),
```

**File:** engine-precompiles/src/native.rs (L413-416)
```rust
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
```

**File:** engine-precompiles/src/native.rs (L875-878)
```rust
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_ethereum::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
```

**File:** engine-precompiles/src/utils.rs (L24-29)
```rust
pub fn validate_no_value_attached_to_precompile(value: U256) -> Result<(), ExitError> {
    if value > U256::zero() {
        // don't attach native token value to that precompile
        return Err(ExitError::Other(Borrowed("ATTACHED_VALUE_ERROR")));
    }
    Ok(())
```

**File:** engine/src/accounting.rs (L34-40)
```rust
    pub fn net(&self) -> Net {
        match self.gained.cmp(&self.lost) {
            Ordering::Equal => Net::Zero,
            Ordering::Greater => Net::Gained(self.gained - self.lost),
            Ordering::Less => Net::Lost(self.lost - self.gained),
        }
    }
```
