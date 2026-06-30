### Title
Unpermissioned `deploy_erc20_token` Allows Any Caller to Register Malicious NEP-141 as ERC-20 Bridge Pair, Enabling Unbacked Token Minting — (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

`deploy_erc20_token` in `engine/src/contract_methods/connector.rs` has **no caller access control**. Any NEAR account can invoke it to permanently register an arbitrary NEP-141 account ID as a bridged ERC-20 pair. Because `ft_on_transfer` unconditionally mints ERC-20 tokens for whichever NEP-141 account calls it, a malicious NEP-141 contract registered this way can mint unbounded ERC-20 tokens on Aurora without backing, which can then be sold against real liquidity.

---

### Finding Description

`deploy_erc20_token` only checks `require_running` (contract not paused) before proceeding: [1](#0-0) 

There is no `require_owner_only` or any other caller restriction. The comment inside the `WithMetadata` branch even acknowledges the intent:

> "this transaction could be executed by the owner of the contract only"

but this is never enforced in code. [2](#0-1) 

Contrast this with `mirror_erc20_token`, which correctly gates on `require_owner_only`: [3](#0-2) 

Once a NEP-141 account is registered, `register_token` prevents any future re-registration: [4](#0-3) 

There is no `unregister_token` or update path anywhere in the codebase. The mapping is permanent.

`ft_on_transfer` mints ERC-20 tokens for any predecessor that is not the ETH connector, using the caller-supplied `amount` field verbatim: [5](#0-4) 

`receive_erc20_tokens` is called with `predecessor_account_id` as the NEP-141 identity and `args.amount` as the mint quantity. There is no on-chain verification that the NEP-141 contract actually holds or transferred those tokens to Aurora before calling `ft_on_transfer`.

The ERC-20 bytecode deployed is the engine's own `EvmErc20.bin` / `EvmErc20V2.bin`: [6](#0-5) 

The ERC-20 contract's admin is set to the Aurora engine address, so only the engine can call `mint`. The engine calls `mint` inside `receive_erc20_tokens` whenever `ft_on_transfer` is invoked by the registered NEP-141 predecessor — with no balance cross-check against the NEP-141 contract's actual state.

---

### Impact Explanation

**Critical — Direct theft of user funds.**

Attack path:

1. Attacker deploys `evil.near`, a malicious NEP-141 contract that can call `ft_on_transfer` on Aurora with an arbitrary `amount` without actually holding or transferring tokens.
2. Attacker calls `deploy_erc20_token` on Aurora with `evil.near` as the NEP-141 argument. No access control blocks this. An ERC-20 token (`EvilToken`) is deployed on Aurora and permanently mapped to `evil.near`.
3. `evil.near` calls `ft_on_transfer` on Aurora with `sender_id: attacker`, `amount: 10_000_000`, `msg: <attacker_evm_address>`. Aurora's `ft_on_transfer` sees `predecessor == evil.near` (a registered NEP-141), calls `receive_erc20_tokens`, and mints 10,000,000 `EvilToken` ERC-20 tokens to the attacker's EVM address — with zero real backing.
4. Attacker creates a DEX liquidity pool pairing `EvilToken` with a real asset (e.g., ETH or USDC) on Aurora, or lists it on an existing AMM. Attacker dumps the free-minted tokens into the pool, draining real assets from liquidity providers.
5. The mapping cannot be revoked. The affected ERC-20 address is permanently associated with `evil.near`, and no legitimate project can ever claim that slot.

Secondary impact: if the attacker front-runs a legitimate project's registration of their NEP-141 token, that token is **permanently unbridgeable** through Aurora's canonical bridge.

---

### Likelihood Explanation

**High.** The entry point is a public NEAR contract method callable by any account with no deposit requirement beyond gas. Deploying a NEAR contract costs a small amount of NEAR for storage. The attack requires no privileged access, no leaked keys, and no social engineering. The only prerequisite is knowing the Aurora contract ID and the Borsh encoding of `DeployErc20TokenArgs`.

---

### Recommendation

1. **Add `require_owner_only` to `deploy_erc20_token`**, matching the pattern used in `mirror_erc20_token` and other privileged methods.
2. **Alternatively**, implement an allowlist of pre-approved NEP-141 account IDs that may be registered.
3. **Add a deregistration / emergency-pause mechanism** for individual NEP-141 ↔ ERC-20 pairs so that a mistakenly or maliciously registered pair can be disabled without affecting the rest of the bridge.
4. Remove or correct the misleading comment at line 148–149 that implies owner-only enforcement that does not exist.

---

### Proof of Concept

```
# Step 1: Deploy evil.near — a NEAR contract that:
#   - Implements ft_on_transfer (returns 0, keeping all tokens)
#   - Has a method `attack` that directly calls ft_on_transfer on Aurora
#     with an arbitrary amount and the attacker's EVM address as msg

# Step 2: Register evil.near as a bridge pair (no access control)
near call aurora.near deploy_erc20_token \
  --args <borsh(DeployErc20TokenArgs::Legacy("evil.near"))> \
  --accountId attacker.near

# Step 3: evil.near calls ft_on_transfer on Aurora, minting 10M tokens
# (called from within evil.near's `attack` method, so predecessor = evil.near)
near call aurora.near ft_on_transfer \
  --args '{"sender_id":"attacker.near","amount":"10000000","msg":"<attacker_evm_hex_address>"}' \
  --accountId evil.near   # predecessor_account_id == evil.near → receive_erc20_tokens path

# Step 4: Attacker now holds 10,000,000 EvilToken ERC-20 on Aurora
# Attacker swaps them on a DEX for real ETH/USDC, draining the pool
```

The `ft_on_transfer` branch at line 84 executes `receive_erc20_tokens` because `predecessor_account_id ("evil.near") != get_connector_account_id(...)`. The engine mints the full `args.amount` to the EVM address in `args.msg` with no balance verification against `evil.near`'s actual NEP-141 state. [7](#0-6)

### Citations

**File:** engine/src/contract_methods/connector.rs (L62-109)
```rust
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

**File:** engine/src/contract_methods/connector.rs (L111-125)
```rust
#[named]
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
```

**File:** engine/src/contract_methods/connector.rs (L148-150)
```rust
                // Safe because these promises are read-only calls to the main engine contract
                // and this transaction could be executed by the owner of the contract only.
                let promise_args = PromiseWithCallbackArgs { base, callback };
```

**File:** engine/src/contract_methods/connector.rs (L456-463)
```rust
pub fn mirror_erc20_token<I: IO + Env + Copy, H: PromiseHandler>(
    io: I,
    handler: &mut H,
) -> Result<(), ContractError> {
    let state = state::get_state(&io)?;
    require_running(&state)?;
    // TODO: Add an admin access list of accounts allowed to do it.
    require_owner_only(&state, &io.predecessor_account_id())?;
```

**File:** engine/src/engine.rs (L722-741)
```rust
    pub fn register_token(
        &mut self,
        erc20_token: Address,
        nep141_token: AccountId,
    ) -> Result<(), RegisterTokenError> {
        match get_erc20_from_nep141(&self.io, &nep141_token) {
            Err(GetErc20FromNep141Error::Nep141NotFound) => (),
            Err(GetErc20FromNep141Error::InvalidNep141AccountId) => {
                return Err(RegisterTokenError::InvalidNep141AccountId);
            }
            Err(GetErc20FromNep141Error::InvalidAddress) => {
                return Err(RegisterTokenError::InvalidAddress);
            }
            Ok(_) => return Err(RegisterTokenError::TokenAlreadyRegistered),
        }

        let erc20_token = ERC20Address(erc20_token);
        let nep141_token = NEP141Account(nep141_token);
        nep141_erc20_map(self.io).insert(&nep141_token, &erc20_token);
        Ok(())
```

**File:** engine/src/engine.rs (L1317-1337)
```rust
pub fn setup_deploy_erc20_input(
    current_account_id: &AccountId,
    erc20_metadata: Option<Erc20Metadata>,
) -> Vec<u8> {
    #[cfg(feature = "error_refund")]
    let erc20_contract = include_bytes!("../../etc/eth-contracts/res/EvmErc20V2.bin");
    #[cfg(not(feature = "error_refund"))]
    let erc20_contract = include_bytes!("../../etc/eth-contracts/res/EvmErc20.bin");

    let erc20_admin_address = current_address(current_account_id);
    let erc20_metadata = erc20_metadata.unwrap_or_default();

    let deploy_args = ethabi::encode(&[
        ethabi::Token::String(erc20_metadata.name),
        ethabi::Token::String(erc20_metadata.symbol),
        ethabi::Token::Uint(erc20_metadata.decimals.into()),
        ethabi::Token::Address(erc20_admin_address.raw().0.into()),
    ]);

    [erc20_contract, deploy_args.as_slice()].concat()
}
```
