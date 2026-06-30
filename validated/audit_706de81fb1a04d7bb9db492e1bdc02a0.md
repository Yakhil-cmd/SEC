### Title
ETH Burned via `SELFDESTRUCT(self)` Is Never Reflected in Connector `totalSupply`, Causing Permanent Insolvency - (File: `engine/src/engine.rs`)

---

### Summary

When an EVM contract self-destructs with itself as the beneficiary (`SELFDESTRUCT(address(this))`), the ETH balance is permanently erased from the EVM state. The ETH connector's NEP-141 `totalSupply` is never decremented to match. This creates a permanent, growing divergence between the connector's reported supply and the actual ETH backing it — a classic "last withdrawer" insolvency.

---

### Finding Description

The `ApplyBackend::apply()` implementation in `engine/src/engine.rs` processes all EVM state changes after each transaction. When a contract is deleted (via `SELFDESTRUCT`), the `Apply::Delete` branch fires:

```rust
Apply::Delete { address } => {
    let current_basic = self.basic(address);
    accounting.remove(current_basic.balance);   // records the loss locally
    let address = Address::new(address);
    let generation = get_generation(&self.io, &address);
    remove_account(&mut self.io, &address, generation);  // wipes EVM state
    writes_counter += 1;
}
``` [1](#0-0) 

After iterating all applies, the code checks the net accounting result:

```rust
match accounting.net() {
    // Net loss is possible if `SELFDESTRUCT(self)` calls are made.
    accounting::Net::Zero | accounting::Net::Lost(_) => (),
    ...
}
``` [2](#0-1) 

The `accounting` struct is a **local, ephemeral variable** — it is never persisted and never used to update the ETH connector's NEP-141 `totalSupply`. The `Net::Lost` arm is explicitly a no-op. The codebase itself documents this as the known consequence of `SELFDESTRUCT(self)`. [3](#0-2) 

The ETH connector's `totalSupply` is only ever modified through two paths:

1. **Deposit** — `ft_on_transfer` → `receive_base_tokens` → `set_balance` (mints EVM ETH, connector mints NEP-141).
2. **Withdrawal** — `withdraw` → `engine_withdraw` on the connector (burns NEP-141, releases ETH on Ethereum side). [4](#0-3) [5](#0-4) 

`SELFDESTRUCT(self)` triggers neither path. The EVM account is deleted and its ETH balance is zeroed, but the connector's NEP-141 `totalSupply` remains unchanged.

The existing test `test_total_supply_accounting` in `engine-tests/src/tests/sanity.rs` explicitly exercises the `SELFDESTRUCT(self)` case but makes **no assertion** that the connector's `totalSupply` is decremented afterward — confirming the gap is untested and unmitigated. [6](#0-5) 

---

### Impact Explanation

After one or more `SELFDESTRUCT(self)` calls burn ETH:

- **Connector `totalSupply`** = original deposited amount (unchanged).
- **Sum of all EVM balances** = original amount − burned amount (reduced).

The connector now reports more ETH than exists in the EVM. When users attempt to withdraw:

- Early withdrawers succeed and receive their full amount.
- Late withdrawers find the EVM has insufficient ETH to back their NEP-141 claims, and their withdrawals fail or are underfunded.

This is permanent, protocol-level insolvency with no recovery mechanism. Impact: **Critical — Insolvency**.

---

### Likelihood Explanation

Post-EIP-6780 (Cancun), `SELFDESTRUCT` only deletes an account (and burns ETH when self-targeting) if the contract was created **in the same transaction**. This is achievable by any EVM user via a factory contract that deploys a child contract with ETH and immediately calls `SELFDESTRUCT(address(this))` in the constructor or a single atomic call. The `test_self_destruct_with_submit` test (not ignored) confirms this path remains live in Aurora Engine. [7](#0-6) 

The attacker needs no special privileges — only the ability to submit EVM transactions. Likelihood: **Medium** (requires deliberate construction, but is fully permissionless).

---

### Recommendation

When `Apply::Delete` is processed and `accounting.net()` returns `Net::Lost(amount)`, the engine must propagate that loss to the ETH connector by decrementing the NEP-141 `totalSupply` by the burned amount. This requires a cross-contract call (or a deferred promise) to the connector's burn/adjust-supply endpoint, analogous to how `engine_withdraw` decrements supply on normal withdrawals.

Alternatively, `SELFDESTRUCT(self)` can be explicitly blocked at the EVM level to prevent ETH from being burned without a corresponding connector update.

---

### Proof of Concept

1. Alice deposits 100 ETH to Aurora EVM via the bridge.
   - Connector `totalSupply` = 100; Alice's EVM balance = 100.
2. Alice deploys a factory contract `F`. `F.deploy()` creates child contract `C` with `value = 50 ETH` and immediately calls `C.selfdestruct(address(C))` — all in one transaction (satisfying EIP-6780).
   - EVM: Alice's balance = 50; `C`'s balance = 0 (deleted). Connector `totalSupply` = 100 (unchanged).
3. Alice calls `withdraw(50)` — succeeds. Connector `totalSupply` = 50.
4. Bob, who also deposited 50 ETH (connector shows his 50 NEP-141 tokens), calls `withdraw(50)`.
   - The EVM has 0 ETH remaining; the connector still reports 50 outstanding NEP-141 tokens.
   - Bob's withdrawal fails or is underfunded. Bob absorbs the loss from Alice's burned ETH.

The root cause is the silent `Net::Lost(_) => ()` arm in `apply()` combined with the absence of any connector notification when ETH is destroyed inside the EVM. [8](#0-7) [9](#0-8)

### Citations

**File:** engine/src/engine.rs (L773-789)
```rust
    pub fn receive_base_tokens(
        &mut self,
        args: &FtOnTransferArgs,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
        let amount = Wei::new_u128(args.amount.as_u128());
        let receipient = message_data.recipient;
        let balance = get_balance(&self.io, &receipient);
        let new_balance = balance
            .checked_add(amount)
            .ok_or(errors::ERR_BALANCE_OVERFLOW)?;

        set_balance(&mut self.io, &receipient, &new_balance);

        sdk::log!("Mint {amount} base tokens for: {}", receipient.encode());

        Ok(None)
```

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

**File:** engine/src/accounting.rs (L4-12)
```rust
/// This struct tracks changes to the supply of a U256 quantity.
/// It is used in our code to keep track of the total supply of ETH on Aurora.
/// This struct is intentionally designed to avoid doing subtraction as much as possible
/// to avoid complexities of signed values and over/underflow.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Accounting {
    gained: U256,
    lost: U256,
}
```

**File:** engine/src/accounting.rs (L29-40)
```rust
    pub fn remove(&mut self, amount: U256) {
        self.lost = self.lost.saturating_add(amount);
    }

    #[must_use]
    pub fn net(&self) -> Net {
        match self.gained.cmp(&self.lost) {
            Ordering::Equal => Net::Zero,
            Ordering::Greater => Net::Gained(self.gained - self.lost),
            Ordering::Less => Net::Lost(self.lost - self.gained),
        }
    }
```

**File:** engine/src/contract_methods/connector.rs (L43-58)
```rust
pub fn withdraw<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;
    env.assert_one_yocto()?;

    let args: WithdrawCallArgs = io.read_input_borsh()?;
    let args = borsh::to_vec(&EngineWithdrawCallArgs {
        sender_id: env.predecessor_account_id(),
        recipient_address: args.recipient_address,
        amount: args.amount,
    })
    .unwrap();

    return_promise(io, env, "engine_withdraw", args, ONE_YOCTO)
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

**File:** engine-tests/src/tests/self_destruct_state.rs (L43-62)
```rust
#[test]
fn test_self_destruct_with_submit() {
    let mut signer = utils::Signer::random();
    let mut runner = utils::deploy_runner();

    let sd_factory_ctr = SelfDestructFactoryConstructor::load();
    let nonce = signer.use_nonce();
    let sd_factory: SelfDestructFactory = runner
        .deploy_contract(&signer.secret_key, |ctr| ctr.deploy(nonce), sd_factory_ctr)
        .into();

    let sd_contract_addr = sd_factory.deploy(&mut runner, &mut signer);

    let sd: SelfDestruct = SelfDestructConstructor::load()
        .0
        .deployed_at(sd_contract_addr)
        .into();

    sd.finish_using_submit(&mut runner, &mut signer);
}
```
