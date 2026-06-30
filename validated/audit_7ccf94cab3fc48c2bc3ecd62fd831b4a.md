### Title
Silo Whitelist Blocks Exit Precompile Calls, Permanently Freezing Funds for Delisted EVM Addresses — (`engine/src/engine.rs`)

### Summary

In Silo mode, the `assert_access` function gates every EVM transaction submission through the `Address` and `Account` whitelists. This includes calls to the `exitToNear` and `exitToEthereum` precompiles — the only on-chain paths for a user to withdraw EVM-held funds. However, the deposit path (`ft_on_transfer` → `receive_base_tokens`) credits ETH balances with no whitelist check. A user whose EVM address is removed from the whitelist after depositing, or whose address was never whitelisted when the whitelist was enabled retroactively, permanently loses access to their funds.

### Finding Description

**Deposit path — no whitelist check:**

`ft_on_transfer` in `engine/src/contract_methods/connector.rs` calls `engine.receive_base_tokens` for ETH deposits: [1](#0-0) 

`receive_base_tokens` directly credits the EVM balance with no whitelist gate: [2](#0-1) 

**Withdrawal path — whitelist enforced on every EVM transaction:**

`assert_access` is called for every submitted EVM transaction. For any transaction with a `to` address (including calls to the `exitToNear` and `exitToEthereum` precompile addresses), it calls `silo::is_allow_submit`: [3](#0-2) 

`is_allow_submit` requires both the NEAR `Account` whitelist and the EVM `Address` whitelist to pass: [4](#0-3) 

Each whitelist check returns `false` (blocked) when the list is enabled and the entry is absent: [5](#0-4) 

The `exitToNear` and `exitToEthereum` precompiles are registered at fixed addresses and are invoked via normal EVM `CALL` transactions, so they are unconditionally subject to `assert_access`: [6](#0-5) 

There is no alternative withdrawal path that bypasses the whitelist check.

**Two concrete freeze scenarios (mirroring the external report exactly):**

1. User deposits ETH via `ft_on_transfer` while whitelisted → operator removes the user's EVM address from the `Address` whitelist → user can no longer call `exitToNear` or `exitToEthereum` → funds permanently frozen.
2. User deposits ETH while the whitelist is disabled → operator enables the `Address` whitelist without including the user's address → same outcome.

### Impact Explanation

Funds (ETH base tokens, and ERC-20 tokens when no fallback address is configured) held at an EVM address that is not present in the enabled `Address` whitelist are permanently unrecoverable by the owner. There is no alternative exit path. This is a **permanent freezing of funds**.

### Likelihood Explanation

Silo mode with whitelists is an explicitly supported production configuration. The `remove_entry_from_whitelist` and `set_whitelist_status` functions are owner-callable at any time with no time-lock. A routine administrative action (delisting a user, enabling the whitelist on an existing deployment) is sufficient to trigger the freeze. No attacker action is required beyond having deposited funds before the whitelist change.

### Recommendation

The `exitToNear` and `exitToEthereum` precompile addresses should be exempted from the `assert_access` whitelist check. Specifically, in `assert_access` (`engine/src/engine.rs`), when `transaction.to` matches `exit_to_near::ADDRESS` or `exit_to_ethereum::ADDRESS`, the whitelist check should be skipped, allowing any address to call the exit precompiles regardless of whitelist status. This mirrors the recommendation in the external report: withdrawal of already-deposited funds must not be restricted by access-control lists that can change after the deposit.

### Proof of Concept

1. Deploy Aurora Engine in Silo mode; enable the `Address` whitelist (`set_whitelist_status` with `WhitelistKind::Address, active: true`).
2. Add user address `U` to the whitelist.
3. User deposits ETH via `ft_on_transfer` → `receive_base_tokens` credits `U`'s EVM balance (no whitelist check).
4. Operator calls `remove_entry_from_whitelist` for address `U`.
5. User attempts to call `exitToNear` precompile at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` via `submit`.
6. `assert_access` calls `is_allow_submit` → `is_address_allowed` → `Address` whitelist is enabled, `U` is absent → returns `false` → `EngineErrorKind::NotAllowed`.
7. User's ETH balance remains in the EVM with no callable exit path. [7](#0-6) [8](#0-7)

### Citations

**File:** engine/src/contract_methods/connector.rs (L81-83)
```rust
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
```

**File:** engine/src/engine.rs (L773-790)
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
    }
```

**File:** engine/src/engine.rs (L1756-1775)
```rust
fn assert_access<I: IO + Copy, E: Env>(
    io: &I,
    env: &E,
    transaction: &NormalizedEthTransaction,
) -> Result<(), EngineError> {
    let allowed = if transaction.to.is_some() {
        silo::is_allow_submit(io, &env.predecessor_account_id(), &transaction.address)
    } else {
        silo::is_allow_deploy(io, &env.predecessor_account_id(), &transaction.address)
    };

    if !allowed {
        return Err(EngineError {
            kind: EngineErrorKind::NotAllowed,
            gas_used: 0,
        });
    }

    Ok(())
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L135-138)
```rust
/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L155-163)
```rust
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}

fn is_account_allowed<I: IO + Copy>(io: &I, account: &AccountId) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Account);
    !list.is_enabled() || list.is_exist(account)
}
```

**File:** engine-precompiles/src/lib.rs (L131-157)
```rust
impl<I: IO + Copy, E: Env, H: ReadOnlyPromiseHandler> executor::stack::PrecompileSet
    for Precompiles<'_, I, E, H>
{
    fn execute(
        &self,
        handle: &mut impl PrecompileHandle,
    ) -> Option<Result<executor::stack::PrecompileOutput, PrecompileFailure>> {
        let address = Address::new(handle.code_address());

        if self.is_paused(&address) {
            return Some(Err(PrecompileFailure::Fatal {
                exit_status: ExitFatal::Other(prelude::Cow::Borrowed("ERR_PAUSED")),
            }));
        }

        let result = match self.all_precompiles.get(&address)? {
            AllPrecompiles::ExitToNear(p) => process_precompile(p, handle),
            AllPrecompiles::ExitToEthereum(p) => process_precompile(p, handle),
            AllPrecompiles::PredecessorAccount(p) => process_precompile(p, handle),
            AllPrecompiles::PrepaidGas(p) => process_precompile(p, handle),
            AllPrecompiles::PromiseResult(p) => process_precompile(p, handle),
            AllPrecompiles::CrossContractCall(p) => process_handle_based_precompile(p, handle),
            AllPrecompiles::Generic(p) => process_precompile(p.as_ref(), handle),
        };

        Some(result.and_then(|output| post_process(output, handle)))
    }
```

**File:** engine-precompiles/src/native.rs (L270-278)
```rust
pub mod exit_to_near {
    use crate::prelude::types::{Address, make_address};

    /// Exit to NEAR precompile address
    ///
    /// Address: `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`
    /// This address is computed as: `&keccak("exitToNear")[12..]`
    pub const ADDRESS: Address = make_address(0xe9217bc7, 0x0b7ed1f598ddd3199e80b093fa71124f);
}
```
