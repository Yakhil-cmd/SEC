### Title
Missing Back-Reference Validation in `set_eth_connector_contract_account` Allows Engine/Connector Account ID Mismatch - (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

`set_eth_connector_contract_account` allows the engine owner to point the engine at a new ETH connector contract without verifying that the new connector's stored `aurora_engine_account_id` matches the current engine's own account ID. If a mismatched connector is registered, every subsequent user `withdraw` call will fail because the connector will reject the engine's cross-contract `engine_withdraw` call with "Method can be called only by aurora engine", permanently freezing all ETH withdrawals from Aurora until the owner corrects the configuration.

---

### Finding Description

The engine stores the address of the external ETH connector contract in its own storage and uses it to route withdrawal cross-contract calls. The owner (or a private caller) can update this stored address at any time via `set_eth_connector_contract_account`.

The implementation in `engine/src/contract_methods/connector.rs` is:

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
        set_connector_account_id(io, &args.account);                          // ← no validation
        set_connector_withdraw_serialization_type(io, &args.withdraw_serialize_type);
        Ok(())
    })
}
``` [1](#0-0) 

The function unconditionally writes the supplied account ID to storage via `set_connector_account_id` with no cross-contract check that the target contract's `aurora_engine_account_id` field equals the current engine's own account ID. [2](#0-1) 

The ETH connector contract enforces a strict caller check on its `engine_withdraw` method: it only accepts calls whose `predecessor_account_id` equals the `aurora_engine_account_id` it was initialized with. This is confirmed by the integration test:

```
assert!(contract.check_error_message(&res, "Method can be called only by aurora engine")?);
``` [3](#0-2) 

The withdrawal flow is:

1. User calls `withdraw` on the engine contract.
2. Engine issues a cross-contract call to `engine_withdraw` on the stored connector account.
3. Connector checks `predecessor_account_id == aurora_engine_account_id`.
4. If the connector was initialized for a **different** engine instance, step 3 fails. [4](#0-3) 

The connector's `aurora_engine_account_id` is set at connector initialization time (the `new` call), as shown in the workspace deployment helper:

```rust
let init_args = json!({
    "aurora_engine_account_id": aurora.id(),
    ...
});
``` [5](#0-4) 

If the owner calls `set_eth_connector_contract_account` with a connector that was initialized for a different engine account (e.g., a testnet connector pointed at a mainnet engine, or a connector deployed during a migration with the wrong engine ID), the engine's `aurora_engine_account_id` will not match the connector's stored value, and all `engine_withdraw` cross-contract calls will be rejected.

The `SetEthConnectorContractAccountArgs` struct carries only the account ID and serialization type — no back-reference field is present or checked: [6](#0-5) 

---

### Impact Explanation

All user-initiated ETH withdrawals from Aurora to NEAR are routed through `engine_withdraw` on the connector. A mismatched connector causes every such call to fail at the NEAR receipt level. User EVM balances remain locked inside the engine — they cannot be redeemed for native ETH on NEAR. This constitutes a **temporary (potentially extended) freeze of all bridged ETH funds** for every user of the engine instance. The freeze persists until the owner issues a corrective `set_eth_connector_contract_account` call, which may not happen quickly if the misconfiguration is not immediately detected.

**Impact: High — Temporary freezing of funds (all ETH withdrawals broken).**

---

### Likelihood Explanation

The scenario is realistic during routine operational events:

- **Connector migration**: A new connector version is deployed and initialized, but the `aurora_engine_account_id` is set to a staging or old engine account by mistake.
- **Multi-engine deployments**: An operator managing multiple Aurora engine instances (mainnet, testnet, silo) accidentally registers a connector belonging to a different instance.
- **Upgrade flows**: The connector is redeployed as part of an upgrade; the initialization arguments are copy-pasted from a different environment.

No malicious actor is required. The owner acts in good faith but supplies an incorrect account ID. The function provides no guard rail to catch the mistake at write time.

**Likelihood: Medium.**

---

### Recommendation

Before persisting the new connector account ID, the engine should verify that the candidate connector's `aurora_engine_account_id` equals the engine's own current account ID. This can be done by:

1. Adding a view call to the candidate connector (e.g., `get_aurora_engine_account_id`) as a prerequisite cross-contract check before the write, or
2. Requiring the caller to supply the expected `aurora_engine_account_id` as part of `SetEthConnectorContractAccountArgs` and asserting it equals `env.current_account_id()` before storing.

This mirrors the pattern recommended in the original Y2K report: verify the back-reference at the time of the configuration change, not after the fact.

---

### Proof of Concept

1. Deploy Engine A at account `engine-a.near` with connector `connector-a.near` (initialized with `aurora_engine_account_id = "engine-a.near"`).
2. Deploy a second connector `connector-b.near` initialized with `aurora_engine_account_id = "engine-b.near"` (a different engine).
3. Engine A owner calls `set_eth_connector_contract_account` with `account = "connector-b.near"`. The call succeeds — no validation occurs.
4. User calls `withdraw` on `engine-a.near`.
5. Engine A issues a cross-contract call to `connector-b.near::engine_withdraw(...)` with `predecessor_account_id = "engine-a.near"`.
6. `connector-b.near` checks `"engine-a.near" == "engine-b.near"` → **false** → panics with "Method can be called only by aurora engine".
7. The withdrawal receipt fails. The user's EVM balance remains locked. All subsequent withdrawals by all users fail identically until the owner corrects the connector registration. [1](#0-0) [7](#0-6)

### Citations

**File:** engine/src/contract_methods/connector.rs (L417-438)
```rust
#[named]
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

**File:** engine-tests-connector/src/connector.rs (L425-432)
```rust
    let res = user_acc
        .call(contract.eth_connector_contract.id(), "engine_withdraw")
        .args_borsh((user_acc.id(), *RECIPIENT_ADDRESS, withdraw_amount))
        .deposit(ONE_YOCTO)
        .transact()
        .await?;
    assert!(res.is_failure());
    assert!(contract.check_error_message(&res, "Method can be called only by aurora engine")?);
```

**File:** engine-tests-connector/src/connector.rs (L500-507)
```rust
    let res = user_acc
        .call(contract.engine_contract.id(), "withdraw")
        .args_borsh((*RECIPIENT_ADDRESS, withdraw_amount))
        .max_gas()
        .deposit(ONE_YOCTO)
        .transact()
        .await?;
    assert!(res.is_success());
```

**File:** engine-tests/src/utils/workspace.rs (L67-72)
```rust
    let init_args = json!({
        "metadata": metadata,
        "aurora_engine_account_id": aurora.id(),
        "owner_id": contract_account.id(),
        "controller": aurora.id()
    });
```

**File:** engine-types/src/parameters/connector.rs (L214-218)
```rust
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Eq)]
pub struct SetEthConnectorContractAccountArgs {
    pub account: AccountId,
    pub withdraw_serialize_type: WithdrawSerializeType,
}
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
