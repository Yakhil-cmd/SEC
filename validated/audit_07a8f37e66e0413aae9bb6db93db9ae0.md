### Title
ETH Burned via `SELFDESTRUCT(self)` Not Reflected in NEP-141 `ft_total_supply`, Permanently Freezing Bridged ETH on Ethereum - (`engine/src/engine.rs`)

---

### Summary

When an EVM contract self-destructs with itself as the beneficiary (`SELFDESTRUCT(self)`), the ETH balance is permanently destroyed in the EVM state. The engine's `ApplyBackend::apply()` correctly records this as a net loss via the `Accounting` struct, but **never decrements the NEP-141 `ft_total_supply`** managed by the external eth connector contract. This creates a permanent divergence: the eth connector believes more ETH is in circulation than actually exists in the EVM, and the corresponding ETH locked on Ethereum can never be unlocked.

---

### Finding Description

The `ApplyBackend` implementation in `engine/src/engine.rs` processes EVM state changes after each transaction. When a contract self-destructs, SputnikVM emits an `Apply::Delete { address }` variant. The engine handles this at lines 2046–2053:

```rust
Apply::Delete { address } => {
    let current_basic = self.basic(address);
    accounting.remove(current_basic.balance);   // records ETH as lost
    let address = Address::new(address);
    let generation = get_generation(&self.io, &address);
    remove_account(&mut self.io, &address, generation);
    writes_counter += 1;
}
```

After all applies are processed, the net accounting result is checked at lines 2057–2059:

```rust
match accounting.net() {
    // Net loss is possible if `SELFDESTRUCT(self)` calls are made.
    accounting::Net::Zero | accounting::Net::Lost(_) => (),
    ...
}
```

The `Net::Lost` arm is a no-op — the engine acknowledges the ETH is gone but takes no corrective action on the NEP-141 layer.

The `ft_total_supply` is owned by the external eth connector contract. It is only decremented when:
- `withdraw` is called (ETH withdrawn to Ethereum via the bridge), or
- The `ExitToNear` / `ExitToEthereum` precompiles are invoked (which trigger NEP-141 burns via cross-contract calls).

Neither path is triggered by `SELFDESTRUCT`. The engine's `apply()` function has no mechanism to issue a cross-contract call to the connector to burn the orphaned NEP-141 tokens.

The `ft_total_supply` function in `engine/src/contract_methods/connector.rs` simply proxies to the connector:

```rust
pub fn ft_total_eth_supply_on_near<I: IO + Copy + PromiseHandler + Env>(
    io: I,
) -> Result<(), ContractError> {
    return_promise(io, &io, "ft_total_supply", Vec::new(), ZERO_YOCTO)
}
```

The connector's `ft_total_supply` is never updated when ETH is burned in the EVM.

The existing test `test_total_supply_accounting` in `engine-tests/src/tests/sanity.rs` (lines 154–164) explicitly acknowledges the self-destruct-with-self scenario but makes **no assertion** about `ft_total_supply` after the burn, leaving the discrepancy undetected.

---

### Impact Explanation

**Permanent freezing of funds.**

The deposit flow for ETH to Aurora EVM is:
1. ETH is locked on Ethereum.
2. The eth connector mints NEP-141 tokens (`ft_total_supply` increases).
3. NEP-141 tokens are transferred to the Aurora engine contract via `ft_on_transfer`.
4. The engine credits the target EVM address with ETH balance.

After `SELFDESTRUCT(self)` burns the EVM ETH:
- The EVM balance is zero and the account is deleted.
- The NEP-141 tokens held by the engine contract are **not burned** — `ft_balance_of(engine)` is unchanged.
- The ETH on Ethereum remains locked.

To unlock ETH on Ethereum, a user must call `withdraw` or the `ExitToEthereum` precompile, both of which require a live EVM ETH balance. The "orphaned" NEP-141 tokens inside the engine contract have no corresponding EVM ETH backing them and no mechanism exists to burn them and release the Ethereum-side collateral. The ETH on Ethereum is permanently frozen.

---

### Likelihood Explanation

Any EVM user can trigger this. A contract that is created and self-destructs in the same transaction (which still works post-Cancun EIP-6780, since EIP-6780 only preserves accounts that were **not** created in the same transaction) with itself as beneficiary will burn ETH. Realistic scenarios include:

- DeFi protocols using CREATE2 + SELFDESTRUCT patterns (e.g., one-time-use proxy contracts, flash-loan receivers) that hold ETH and self-destruct with self as beneficiary.
- Any user who accidentally or intentionally deploys a contract with ETH and calls `SELFDESTRUCT(address(this))`.

The attacker-controlled entry path is: submit an EVM transaction via the `submit` method that deploys a contract with ETH value and immediately self-destructs with `address(this)` as beneficiary, all within one transaction.

---

### Recommendation

In the `Net::Lost` branch of `ApplyBackend::apply()`, the engine should issue a cross-contract call to the eth connector to burn the corresponding amount of NEP-141 tokens, mirroring what the `ExitToEthereum` precompile does. Alternatively, the engine could track a "burned ETH" counter in its own storage and subtract it from the reported `ft_total_supply` (analogous to the second mitigation option in the reference report).

---

### Proof of Concept

1. Deposit `X` wei of ETH to Aurora EVM to address `A` (via the bridge).
2. From address `A`, deploy a contract `C` with `value = X` in the same transaction that calls `selfdestruct(payable(address(this)))`.
3. Observe: `get_balance(A) == 0`, `get_balance(C) == 0` (account deleted).
4. Observe: `ft_total_supply()` still returns the pre-burn value (inflated by `X`).
5. The `X` wei of ETH on Ethereum is permanently locked — no `withdraw` or exit precompile call can recover it because no EVM address holds the corresponding balance.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** engine/src/engine.rs (L2046-2059)
```rust
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

**File:** engine/src/accounting.rs (L29-31)
```rust
    pub fn remove(&mut self, amount: U256) {
        self.lost = self.lost.saturating_add(amount);
    }
```

**File:** engine/src/contract_methods/connector.rs (L346-350)
```rust
pub fn ft_total_eth_supply_on_near<I: IO + Copy + PromiseHandler + Env>(
    io: I,
) -> Result<(), ContractError> {
    return_promise(io, &io, "ft_total_supply", Vec::new(), ZERO_YOCTO)
}
```

**File:** engine-tests/src/tests/sanity.rs (L154-165)
```rust
    // Self-destruct with self-benefactor burns any ETH in the destroyed contract
    let contract = deploy_contract(&mut runner, &mut signer);
    let _submit_result = runner
        .submit_with_signer(&mut signer, |nonce| {
            contract.call_method_with_args(
                "destruct",
                &[ethabi::Token::Address(contract.address.raw().0.into())],
                nonce,
            )
        })
        .unwrap();
}
```
