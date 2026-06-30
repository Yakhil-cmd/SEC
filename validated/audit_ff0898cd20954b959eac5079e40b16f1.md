### Title
Updating Eth-Connector Contract Account Without State Validation Causes Insolvency - (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

`set_eth_connector_contract_account` allows the owner to redirect the Aurora Engine's bridge accounting to a new external eth-connector contract without any validation that the new contract's token supply matches the existing EVM state. If the new connector starts with fresh (zero) state, all existing user ETH balances in the EVM become unbacked, causing permanent fund freeze and insolvency. This is the direct structural analog to the reported QuestBoard ID-collision issue: a mutable contract reference is updated, the replacement starts with zero state, and the existing accumulated state is orphaned.

---

### Finding Description

The function `set_eth_connector_contract_account` in `engine/src/contract_methods/connector.rs` simply overwrites the stored connector account ID and serialization type with no cross-checks:

```rust
pub fn set_eth_connector_contract_account<I: IO + Copy, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let is_private = env.assert_private_call();
        if is_private.is_err() {
            require_owner_only(&state, &env.predecessor_account_id())?;
        }
        let args: SetEthConnectorContractAccountArgs = io.read_input_borsh()?;
        set_connector_account_id(io, &args.account);                          // ← blind overwrite
        set_connector_withdraw_serialization_type(io, &args.withdraw_serialize_type);
        Ok(())
    })
}
``` [1](#0-0) 

`set_connector_account_id` writes the new account ID directly to storage under `EthConnectorStorageId::EthConnectorAccount` with no invariant checks: [2](#0-1) 

Every subsequent bridge operation — `withdraw`, `ft_transfer`, `ft_on_transfer`, `finish_deposit`, `ft_total_supply`, etc. — resolves the connector target by reading this key and forwarding the call to whatever account is stored there: [3](#0-2) 

The Aurora Engine's EVM state (user ETH balances) lives in the engine's own NEAR storage and is completely independent of the connector contract's NEP-141 token ledger. The two must be kept in 1:1 correspondence for the bridge to be solvent. When the connector reference is replaced with a freshly deployed contract (total supply = 0, all balances = 0), the EVM balances remain intact but are now backed by nothing in the new connector.

The function is exposed as a public NEAR entry point: [4](#0-3) 

It is callable by the owner or by the contract itself (private call path), as confirmed by the integration test setup that calls it during normal deployment: [5](#0-4) 

---

### Impact Explanation

**Severity: Critical — Permanent Freezing of Funds / Insolvency.**

After the connector reference is replaced with a zero-state contract:

- Every call to `withdraw` / `exitToNear` / `exitToEthereum` is forwarded to the new connector, which holds no tokens and will fail or return zero.
- The old connector still holds the real ETH/NEAR tokens but is no longer reachable through the engine.
- All user EVM ETH balances are permanently unwithdrawable — a complete, irreversible fund freeze for every depositor.
- The bridge is insolvent: total EVM ETH supply > new connector total supply (0).

---

### Likelihood Explanation

The function is the intended upgrade path for the connector contract (e.g., to patch a bug or add features). A well-intentioned owner performing an upgrade — deploying a new connector, then calling `set_eth_connector_contract_account` before migrating state — triggers the vulnerability without any malicious intent. The absence of any guard (total-supply check, migration handshake, or even a warning) makes an accidental insolvency a realistic operational risk. The function is also callable via the private-call path, meaning it can be triggered as part of a multi-step promise chain without a separate owner signature.

---

### Recommendation

1. **Add a total-supply invariant check**: Before accepting the new connector account, query its `ft_total_supply` and assert it equals the engine's current EVM ETH total supply.
2. **Require an explicit migration handshake**: The new connector should acknowledge receipt of the full token supply before the reference is switched.
3. **Consider making the connector immutable** (analogous to the fix applied in the referenced report): if the connector must be replaceable, enforce a two-phase migration with atomic state transfer, or remove the function and replace the connector only via a full engine redeployment.

---

### Proof of Concept

```
1. Alice and Bob each deposit 10 ETH into Aurora.
   → EVM state: Alice = 10 ETH, Bob = 10 ETH (total 20 ETH)
   → Old connector NEP-141 total supply = 20 ETH

2. Owner deploys new_connector (fresh state, total supply = 0).

3. Owner calls:
     set_eth_connector_contract_account({
       account: "new_connector.near",
       withdraw_serialize_type: Borsh
     })
   → Engine now routes all bridge calls to new_connector.near

4. Alice calls exitToNear(10 ETH):
   → Engine calls new_connector.near::engine_withdraw(alice, 10 ETH)
   → new_connector has 0 balance → call fails or returns 0
   → Alice's 10 EVM ETH is permanently frozen

5. Old connector still holds 20 ETH worth of tokens, but the engine
   no longer references it. Funds are permanently inaccessible.
   Bridge is insolvent.
```

### Citations

**File:** engine/src/contract_methods/connector.rs (L418-438)
```rust
pub fn set_eth_connector_contract_account<I: IO + Copy, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let is_private = env.assert_private_call();

        if is_private.is_err() {
            require_owner_only(&state, &env.predecessor_account_id())?;
        }

        let args: SetEthConnectorContractAccountArgs = io.read_input_borsh()?;

        set_connector_account_id(io, &args.account);
        set_connector_withdraw_serialization_type(io, &args.withdraw_serialize_type);

        Ok(())
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L562-567)
```rust
pub fn set_connector_account_id<I: IO + Copy>(mut io: I, account_id: &AccountId) {
    io.write_borsh(
        &construct_contract_key(EthConnectorStorageId::EthConnectorAccount),
        account_id,
    );
}
```

**File:** engine/src/contract_methods/connector.rs (L598-616)
```rust
fn return_promise<I: IO + PromiseHandler, E: Env>(
    mut io: I,
    env: &E,
    method: &str,
    args: Vec<u8>,
    deposit: Yocto,
) -> Result<(), ContractError> {
    let promise_args = PromiseCreateArgs {
        target_account_id: get_connector_account_id(&io)?,
        method: method.to_string(),
        args,
        attached_balance: deposit,
        attached_gas: calculate_attached_gas(env),
    };
    let promise_id = io.promise_create_call(&promise_args);

    io.promise_return(promise_id);

    Ok(())
```

**File:** engine/src/lib.rs (L700-707)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn set_eth_connector_contract_account() {
        let io = Runtime;
        let env = Runtime;
        contract_methods::connector::set_eth_connector_contract_account(io, &env)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```

**File:** engine-tests-connector/src/utils.rs (L170-180)
```rust
        let acc = SetEthConnectorContractAccountArgs {
            account: eth_connector_contract.id().as_str().parse().unwrap(),
            withdraw_serialize_type: WithdrawSerializeType::Borsh,
        };
        let res = engine_contract
            .call("set_eth_connector_contract_account")
            .args_borsh(acc)
            .max_gas()
            .transact()
            .await?;
        assert!(res.is_success());
```
