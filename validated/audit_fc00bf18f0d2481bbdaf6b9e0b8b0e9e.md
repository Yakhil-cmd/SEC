### Title
Unbacked ERC-20 Token Minting via Direct `ft_on_transfer` Call Without `ft_transfer_call` Context Enforcement - (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

The `ft_on_transfer` entrypoint on Aurora Engine can be invoked directly by any NEAR account that is registered as a NEP-141 token in Aurora's registry. Because `deploy_erc20_token` has no caller access control, an attacker can register their own NEP-141 token and then call `ft_on_transfer` directly — without going through the `ft_transfer_call` flow — to mint arbitrary amounts of ERC-20 mirror tokens without depositing any actual NEP-141 tokens. This is the direct analog of the report's bundled-transaction extraction bug: a sub-operation meant to be part of a larger, validated flow passes all individual checks when submitted in isolation.

---

### Finding Description

The intended flow for bridging NEP-141 tokens into Aurora is:

1. User calls `ft_transfer_call` on the NEP-141 contract.
2. The NEP-141 contract atomically transfers tokens to Aurora and calls `ft_on_transfer` on Aurora Engine (with `predecessor_account_id` = the NEP-141 contract).
3. Aurora Engine mints the corresponding ERC-20 tokens.

The `ft_on_transfer` function in `engine/src/contract_methods/connector.rs` performs no check that it is being invoked as part of step 2 of this flow:

```rust
// engine/src/contract_methods/connector.rs:61-109
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        ...
        let args: FtOnTransferArgs = read_json_args(&io)?;
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };
``` [1](#0-0) 

The only guard is whether `predecessor_account_id` equals the connector account (for base-token minting) or is a registered NEP-141 token (for ERC-20 minting). There is no assertion that this call arrived as a NEAR callback from a `ft_transfer_call` receipt.

The second precondition — that the predecessor must be a registered NEP-141 token — is trivially satisfied because `deploy_erc20_token` has **no caller access control**:

```rust
// engine/src/contract_methods/connector.rs:112-159
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        // ← no require_owner_only, no whitelist check
        let bytes = io.read_input().to_vec();
        let args = DeployErc20TokenArgs::deserialize(&bytes)...;
``` [2](#0-1) 

Any NEAR account can register any NEP-141 token. Once registered, `receive_erc20_tokens` will look up the ERC-20 address and call the ERC-20 contract's mint function using the engine's own admin address as the caller:

```rust
// engine/src/engine.rs:796-843
let erc20_token = get_erc20_from_nep141(&self.io, token)?;
let erc20_admin_address = current_address(current_account_id);
let result = self.call(
    &erc20_admin_address,
    &erc20_token,
    Wei::zero(),
    setup_receive_erc20_tokens_input(&recipient, amount),
    u64::MAX, ...
).and_then(submit_result_or_err)?;
``` [3](#0-2) 

The ERC-20 contract accepts mints from the engine admin address unconditionally, so the mint succeeds.

The public entrypoint is exposed with no additional guard:

```rust
// engine/src/lib.rs:602-609
pub extern "C" fn ft_on_transfer() {
    let io = Runtime;
    let env = Runtime;
    let mut handler = Runtime;
    contract_methods::connector::ft_on_transfer(io, &env, &mut handler)
        .map_err(ContractError::msg).sdk_unwrap();
}
``` [4](#0-3) 

---

### Impact Explanation

An attacker mints an unbounded supply of ERC-20 mirror tokens for their registered NEP-141 token without depositing any actual NEP-141 tokens. This:

- **Breaks the 1:1 backing invariant** of the bridge for that token pair (insolvency of the ERC-20 mirror).
- Allows the attacker to **drain DEX liquidity pools** on Aurora that contain the ERC-20 mirror token by selling unbacked tokens into real-asset liquidity.
- Allows the attacker to **use unbacked tokens as collateral** in Aurora lending protocols, borrowing real assets against nothing.

Impact classification: **Critical** — direct theft of user funds held in Aurora DeFi protocols that accept the attacker's ERC-20 mirror token.

---

### Likelihood Explanation

**High.** The attack requires only:

1. Creating a NEAR account (free, permissionless).
2. Calling `deploy_erc20_token` on Aurora Engine (no access control, open to anyone).
3. Calling `ft_on_transfer` directly from that account (any NEAR account can call any public contract method).

No privileged access, no leaked keys, no governance capture, and no external dependency is required. The entire attack is executable by an unprivileged NEAR user.

---

### Recommendation

Enforce that `ft_on_transfer` is only reachable as a NEAR callback from a `ft_transfer_call` receipt. NEAR provides `env::promise_results_count()` and related APIs to detect callback context. Alternatively:

- Require that the predecessor is on an admin-controlled whitelist of approved NEP-141 tokens (add access control to `deploy_erc20_token` or maintain a separate approval registry).
- At minimum, add a check that `promise_results_count() >= 1` inside `ft_on_transfer` to ensure it is being called as a callback, not as a standalone transaction.

---

### Proof of Concept

```
1. Attacker creates NEAR account `attacker_token.near` and deploys a minimal
   NEP-141 contract on it (no real token supply needed).

2. Attacker calls `deploy_erc20_token` on Aurora Engine:
     input = borsh_encode(DeployErc20TokenArgs::Legacy("attacker_token.near"))
   → Aurora Engine creates an ERC-20 mirror contract at address ERC20_ADDR.

3. Attacker calls `ft_on_transfer` on Aurora Engine FROM `attacker_token.near`:
     predecessor_account_id = "attacker_token.near"
     input = json({
       "sender_id": "attacker_token.near",
       "amount":    "1000000000000000000000000",   // arbitrary large amount
       "msg":       "<attacker_evm_address_hex>"
     })

4. Inside ft_on_transfer:
     predecessor_account_id != connector_account_id
     → receive_erc20_tokens("attacker_token.near", args, ...) is called
     → get_erc20_from_nep141 returns ERC20_ADDR  (registered in step 2)
     → engine calls ERC20_ADDR.mint(attacker_evm_address, 1e24) as admin
     → mint succeeds

5. Attacker now holds 1e24 ERC-20 mirror tokens with zero NEP-141 backing.
   Attacker sells into any Aurora DEX pool containing this token,
   draining real-asset liquidity from other LPs.
```

### Citations

**File:** engine/src/contract_methods/connector.rs (L61-109)
```rust
#[named]
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        let mut engine: Engine<_, _> = Engine::new(
            predecessor_address(&predecessor_account_id),
            current_account_id.clone(),
            io,
            env,
        )?;

        sdk::log!("Call ft_on_transfer");

        let args: FtOnTransferArgs = read_json_args(&io)?;
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };

        #[allow(clippy::used_underscore_binding)]
        let amount_to_return = if let Err(_err) = &result {
            sdk::log!("Error in ft_on_transfer: {_err:?}");
            // An error occurred, so we need to return the amount of tokens to the sender.
            args.amount.as_u128()
        } else {
            // Everything is ok, so return 0.
            0
        };

        let output = crate::prelude::format!("\"{amount_to_return}\"");
        io.return_output(output.as_bytes());

        // In case of an error, we just return Ok(None) to avoid a panic in the contract. It's ok
        // because in case of an error, we already returned the amount of tokens to the sender.
        Ok(result.unwrap_or(None))
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L112-130)
```rust
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let bytes = io.read_input().to_vec();
        let args =
            DeployErc20TokenArgs::deserialize(&bytes).map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;

        match args {
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;

                io.return_output(
                    &borsh::to_vec(address.as_bytes()).map_err(|_| errors::ERR_SERIALIZE)?,
                );
                Ok(PromiseOrValue::Value(address))
```

**File:** engine/src/engine.rs (L824-837)
```rust
        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
        let erc20_admin_address = current_address(current_account_id);
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
                u64::MAX,
                Vec::new(), // TODO: are there values we should put here?
                Vec::new(),
                handler,
            )
            .and_then(submit_result_or_err)?;
```

**File:** engine/src/lib.rs (L602-609)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn ft_on_transfer() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::connector::ft_on_transfer(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
```
